#!/usr/bin/env python3
# /srv/calibre-ingest/watched_ingest.py
#
# CWA-Compatible Calibre Ingest Watcher
#
# Features:
# - Reads CWA settings from /opt/calibre-web-nextgen/cwa.db.
# - Reads auto_convert_target_format from cwa_settings exactly as before.
# - Reads auto_ingest_automerge from cwa_settings exactly as before.
# - Passes valid CWA/Calibre automerge modes directly to calibredb.
# - Adds the incoming/original format before attempting conversion.
# - Resolves the resulting Calibre book_id.
# - Inspects ALL formats already attached to that Calibre book.
# - Does not reconvert if the configured target format already exists.
# - Chooses the best available existing format for conversion.
# - Falls back through other available formats if conversion fails.
# - If NO available format can be converted to the configured target,
#   logs that fact and completes the ingest WITHOUT retrying conversion.
# - auto_convert_retained_formats does NOT suppress conversion.
# - auto_convert_ignored_formats excludes formats as conversion sources.
# - Uses resumable retry stages for actual ingest/finalization failures.
# - Prevents retry-queued files from being processed by the normal scanner.
# - Moves successful source/conversion files to processed/.
# - Moves permanently failed source files to failed/.
# - Uses the configured retry_queue_file from dirs.json.
# - Creates the configured temporary conversion directory.
# - Uses unique per-conversion work directories.
# - Uses an end-to-end ingest timeout/deadline.
# - Uses atomic-ish/fsynced retry and status writes.
# - Uses a single-instance flock.
# - Handles filesystem errors without terminating the watcher.
# - Writes metadata-change logs for cover_enforcer.py.
#
# Python 3.8+
# Linux (fcntl/flock used for single-instance locking)

import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path


# ============================================================
# CONFIGURATION FILES
# ============================================================

DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")

STATUS_FILE = Path(
    "/opt/calibre-web-nextgen/config/cwa_ingest_status"
)

RETRY_QUEUE_FILE = Path(
    "/opt/calibre-web-nextgen/config/cwa_ingest_retry_queue"
)

METADATA_CHANGE_LOGS_DIR = Path(
    "/opt/calibre-web-nextgen/config/metadata_change_logs"
)

DEFAULT_LOCK_FILE = Path(
    "/opt/calibre-web-nextgen/config/cwa_ingest_watcher.lock"
)


# ============================================================
# WATCHER CONSTANTS
# ============================================================

POLL_INTERVAL_SECONDS = 10
RETRY_SCAN_EVERY = 5
SETTINGS_LOG_EVERY = 10


# ============================================================
# PIPELINE STAGES
# ============================================================

STAGE_ADD_ORIGINAL = "add_original"
STAGE_CONVERT = "convert"
STAGE_ATTACH_FORMAT = "attach_format"
STAGE_FINALIZE = "finalize"
STAGE_FINALIZE_NO_CONVERSION = "finalize_no_conversion"


# ============================================================
# CALIBRE GLOBALS
# ============================================================

CALIBRE_LIBRARY = ""

EBOOK_CONVERT = ""
CALIBREDB = ""
KEPUBIFY = ""

LOCK_HANDLE = None


# ============================================================
# SUPPORTED CONVERSION INPUTS
# ============================================================

# Mirrors the general format set supported by current CWA/Calibre
# ingest/conversion handling. KFX may require plugins/environment support.
SUPPORTED_BOOK_FORMATS = {
    "acsm",
    "azw",
    "azw3",
    "azw4",
    "cb7",
    "cbc",
    "cbr",
    "cbz",
    "chm",
    "djvu",
    "docx",
    "epub",
    "fb2",
    "fbz",
    "html",
    "htmlz",
    "kepub",
    "kfx",
    "kfx-zip",
    "lit",
    "lrf",
    "mobi",
    "odt",
    "pdb",
    "pdf",
    "pml",
    "prc",
    "rb",
    "rtf",
    "snb",
    "tcr",
    "txt",
    "txtz",
}


# Preferred source order when several formats already exist.
#
# IMPORTANT: this is deliberately a tuple rather than a set so the order
# is deterministic.
CONVERSION_SOURCE_PRIORITY = (
    "epub",
    "kepub",
    "lit",
    "mobi",
    "azw",
    "azw3",
    "fb2",
    "fbz",
    "azw4",
    "prc",
    "odt",
    "lrf",
    "pdb",
    "cbz",
    "pml",
    "rb",
    "cbr",
    "cb7",
    "cbc",
    "chm",
    "djvu",
    "snb",
    "tcr",
    "pdf",
    "docx",
    "rtf",
    "html",
    "htmlz",
    "txtz",
    "txt",
    "acsm",
    "kfx",
    "kfx-zip",
)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_iso():
    return datetime.now().isoformat()


def safe_int(value, default, minimum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default

    if minimum is not None:
        result = max(minimum, result)

    return result


def safe_bool(value, default=False):
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value).strip().lower()

    if text in {"1", "true", "yes", "on", "enabled"}:
        return True

    if text in {"0", "false", "no", "off", "disabled", ""}:
        return False

    return default


def ensure_directory(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def atomic_write_text(path, text):
    """
    Write a file via same-directory temporary file + fsync + replace.
    """
    path = Path(path)

    ensure_directory(path.parent)

    temp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, path)

        # Best-effort directory fsync.
        try:
            flags = os.O_RDONLY

            if hasattr(os, "O_DIRECTORY"):
                flags |= os.O_DIRECTORY

            dir_fd = os.open(str(path.parent), flags)

            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

        except OSError:
            pass

    finally:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass


# ============================================================
# EXECUTABLE DISCOVERY
# ============================================================

def resolve_executable(env_name, command_name):
    """
    Resolve executable reliably under both systemd and interactive shells.
    """
    configured = os.environ.get(env_name, "").strip()

    if configured:
        path = Path(configured)

        if path.is_file() and os.access(path, os.X_OK):
            return str(path)

        print(
            f"[CONFIG] {env_name} points to a non-executable path: "
            f"{configured}"
        )

    found = shutil.which(command_name)

    if found:
        return found

    candidates = [
        Path("/opt/calibre") / command_name,
        Path("/usr/local/bin") / command_name,
        Path("/usr/bin") / command_name,
        Path("/bin") / command_name,
    ]

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    return ""


def configure_calibre_executables():
    """
    Resolve required Calibre commands.

    kepubify is optional unless KEPUB conversion is actually requested.
    """
    global EBOOK_CONVERT, CALIBREDB, KEPUBIFY

    EBOOK_CONVERT = resolve_executable(
        "EBOOK_CONVERT",
        "ebook-convert",
    )

    CALIBREDB = resolve_executable(
        "CALIBREDB",
        "calibredb",
    )

    KEPUBIFY = resolve_executable(
        "KEPUBIFY",
        "kepubify",
    )

    print(
        f"[CALIBRE] ebook-convert: "
        f"{EBOOK_CONVERT or 'NOT FOUND'}"
    )

    print(
        f"[CALIBRE] calibredb:      "
        f"{CALIBREDB or 'NOT FOUND'}"
    )

    print(
        f"[CALIBRE] kepubify:       "
        f"{KEPUBIFY or 'NOT FOUND (only required for KEPUB target)'}"
    )

    missing = []

    if not EBOOK_CONVERT:
        missing.append("ebook-convert")

    if not CALIBREDB:
        missing.append("calibredb")

    if missing:
        raise RuntimeError(
            "Required Calibre executable(s) not found: "
            + ", ".join(missing)
        )


# ============================================================
# DIRECTORY CONFIG
# ============================================================

def load_dirs_config():
    """
    Load paths from CWA dirs.json.
    """
    default_config = {
        "ingest_folder": "/srv/calibre-ingest",
        "calibre_library_dir": "/home/ava/Dusty Bookshelf",
        "tmp_conversion_dir": "/tmp/cwa_conversions",
        "processed_folder": "/srv/calibre-ingest/processed",
        "failed_folder": "/srv/calibre-ingest/failed",
        "retry_queue_file": str(RETRY_QUEUE_FILE),
        "max_retry_attempts": 3,
        "retry_interval_seconds": 300,
        "ingest_lock_file": str(DEFAULT_LOCK_FILE),
    }

    if not DIRS_JSON.exists():
        return default_config

    try:
        with open(DIRS_JSON, "r", encoding="utf-8") as f:
            config = json.load(f)

        if not isinstance(config, dict):
            raise ValueError("dirs.json root must be an object")

        # Preserve compatibility with quoted path values.
        for key, value in list(config.items()):
            if isinstance(value, str):
                value = value.strip()

                if (
                    len(value) >= 2
                    and value.startswith("'")
                    and value.endswith("'")
                ):
                    value = value[1:-1]

                config[key] = value

        merged = {
            **default_config,
            **config,
        }

        required_paths = (
            "ingest_folder",
            "calibre_library_dir",
            "processed_folder",
            "failed_folder",
            "retry_queue_file",
            "ingest_lock_file",
        )

        for key in required_paths:
            if not merged.get(key):
                print(
                    f"[CONFIG] Invalid {key}; using default "
                    f"{default_config[key]}"
                )

                merged[key] = default_config[key]

        return merged

    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(
            f"[CONFIG] Error reading dirs.json: {e}"
        )

        return default_config


# ============================================================
# CWA SETTINGS
# ============================================================

def load_cwa_settings():
    """
    Read settings from:

        /opt/calibre-web-nextgen/cwa.db
        table: cwa_settings

    IMPORTANT:
    This intentionally preserves the original source/location for both:
      - auto_convert_target_format
      - auto_ingest_automerge
    """
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
        conn = sqlite3.connect(
            str(CWA_DB),
            timeout=30,
        )

        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM cwa_settings LIMIT 1"
        )

        row = cursor.fetchone()

        if not row:
            return default_settings

        columns = [
            desc[0]
            for desc in cursor.description
        ]

        return {
            **default_settings,
            **dict(zip(columns, row)),
        }

    except sqlite3.Error as e:
        print(
            f"[SETTINGS] Error reading cwa.db: {e}"
        )

        return default_settings

    finally:
        if conn is not None:
            conn.close()


def get_target_format(settings):
    """
    Return target format configured in cwa.db.
    """
    value = settings.get(
        "auto_convert_target_format",
        "epub",
    )

    target = str(
        value or "epub"
    ).strip().lower().lstrip(".")

    if not target:
        target = "epub"

    return target


def get_automerge_mode(settings):
    """
    Read and normalize auto_ingest_automerge.

    The SOURCE is intentionally unchanged:
        cwa.db -> cwa_settings -> auto_ingest_automerge

    Valid Calibre/CWA modes:
        ignore
        overwrite
        new_record

    Legacy boolean-ish values remain supported.
    """
    raw = settings.get(
        "auto_ingest_automerge",
        0,
    )

    if raw is None:
        return None

    if isinstance(raw, bool):
        return "overwrite" if raw else None

    if isinstance(raw, (int, float)):
        return "overwrite" if raw else None

    value = str(raw).strip().lower()

    if value in {
        "ignore",
        "overwrite",
        "new_record",
    }:
        return value

    if value in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
    }:
        return "overwrite"

    if value in {
        "0",
        "false",
        "no",
        "off",
        "disabled",
        "",
    }:
        return None

    print(
        "[CONFIG] Unknown auto_ingest_automerge value "
        f"{raw!r}; automerge disabled"
    )

    return None


# ============================================================
# SINGLE INSTANCE LOCK
# ============================================================

def acquire_instance_lock(config):
    global LOCK_HANDLE

    lock_path = Path(
        config.get(
            "ingest_lock_file",
            str(DEFAULT_LOCK_FILE),
        )
    )

    ensure_directory(lock_path.parent)

    handle = open(
        lock_path,
        "a+",
        encoding="utf-8",
    )

    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )

    except BlockingIOError:
        handle.close()

        raise RuntimeError(
            "Another ingest watcher already holds "
            f"{lock_path}"
        )

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()

    LOCK_HANDLE = handle

    print(
        f"[LOCK] Acquired watcher lock: "
        f"{lock_path}"
    )


# ============================================================
# STATUS FILE
# ============================================================

def sanitize_status_field(value):
    if value is None:
        return ""

    return (
        str(value)
        .replace("\r", " ")
        .replace("\n", " ")
    )


def write_ingest_status(
    state,
    filename="",
    detail="",
):
    """
    Preserve CWA status format exactly:

        state:filename:YYYY-MM-DD HH:MM:SS:detail
    """
    timestamp = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    filename = sanitize_status_field(filename)
    detail = sanitize_status_field(detail)

    status_line = (
        f"{state}:{filename}:{timestamp}:{detail}"
    )

    try:
        atomic_write_text(
            STATUS_FILE,
            status_line,
        )

        print(
            f"[STATUS] {state.upper()}: "
            f"{filename or 'idle'}"
        )

    except OSError as e:
        print(
            f"[STATUS] Error writing status file: {e}"
        )


# ============================================================
# METADATA CHANGE LOG
# ============================================================

def write_metadata_change_log(
    book_id,
    title,
    authors=None,
    **extra_fields,
):
    """
    Trigger cover_enforcer.py / metadata change detector.
    """
    try:
        METADATA_CHANGE_LOGS_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

    except OSError as e:
        print(
            "[METADATA] Error creating metadata "
            f"log directory: {e}"
        )

        return None

    payload = {
        "book_id": str(book_id),
        "title": title or "",
        "authors": authors or [],
    }

    payload.update(extra_fields)

    timestamp = datetime.now().strftime(
        "%Y%m%d%H%M%S"
    )

    filename = (
        f"{timestamp}-{book_id}-"
        f"{uuid.uuid4().hex[:8]}.json"
    )

    path = (
        METADATA_CHANGE_LOGS_DIR
        / filename
    )

    try:
        atomic_write_text(
            path,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

        print(
            f"[METADATA] Wrote change log: "
            f"{filename}"
        )

        return path

    except OSError as e:
        print(
            f"[METADATA] Error writing change log: {e}"
        )

        return None


# ============================================================
# RETRY QUEUE
# ============================================================

def get_retry_queue_file(config=None):
    if config:
        configured = config.get(
            "retry_queue_file"
        )

        if configured:
            return Path(configured)

    return RETRY_QUEUE_FILE


def load_retry_queue(config=None):
    retry_file = get_retry_queue_file(
        config
    )

    if not retry_file.exists():
        return []

    try:
        with open(
            retry_file,
            "r",
            encoding="utf-8",
        ) as f:
            data = json.load(f)

        if not isinstance(data, list):
            print(
                "[RETRY] Invalid retry queue "
                f"format in {retry_file}"
            )

            return []

        return data

    except (
        json.JSONDecodeError,
        OSError,
    ) as e:
        print(
            f"[RETRY] Error reading queue: {e}"
        )

        return []


def save_retry_queue(
    queue_data,
    config=None,
):
    retry_file = get_retry_queue_file(
        config
    )

    try:
        atomic_write_text(
            retry_file,
            json.dumps(
                queue_data,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
        )

    except OSError as e:
        print(
            f"[RETRY] Error saving queue: {e}"
        )


def find_retry_entry(
    queue_data,
    filename,
):
    for entry in queue_data:
        if (
            isinstance(entry, dict)
            and entry.get("filename") == filename
        ):
            return entry

    return None


def get_retry_entry(
    filename,
    config,
):
    return find_retry_entry(
        load_retry_queue(config),
        filename,
    )


def remove_retry_entry(
    filename,
    config,
):
    queue_data = load_retry_queue(
        config
    )

    new_queue = [
        entry
        for entry in queue_data
        if (
            not isinstance(entry, dict)
            or entry.get("filename") != filename
        )
    ]

    if len(new_queue) != len(queue_data):
        save_retry_queue(
            new_queue,
            config,
        )


def upsert_retry_entry(
    filepath,
    error_msg,
    config,
    stage=STAGE_ADD_ORIGINAL,
    book_id=None,
    converted_path=None,
    attempts=None,
    touch_last_attempt=True,
):
    filepath = Path(filepath)

    queue_data = load_retry_queue(
        config
    )

    entry = find_retry_entry(
        queue_data,
        filepath.name,
    )

    configured_max = safe_int(
        config.get(
            "max_retry_attempts",
            3,
        ),
        3,
        minimum=1,
    )

    if entry is None:
        entry = {
            "filename": filepath.name,
            "original_path": str(filepath),
            "attempts": (
                1
                if attempts is None
                else int(attempts)
            ),
            "max_attempts": configured_max,
            "last_attempt": now_iso(),
            "error": str(error_msg)[:5000],
            "stage": stage,
        }

        queue_data.append(entry)

    else:
        entry["filename"] = filepath.name
        entry["original_path"] = str(filepath)

        entry["attempts"] = (
            safe_int(
                entry.get("attempts", 1),
                1,
                minimum=1,
            )
            if attempts is None
            else int(attempts)
        )

        entry["max_attempts"] = safe_int(
            entry.get(
                "max_attempts",
                configured_max,
            ),
            configured_max,
            minimum=1,
        )

        entry["stage"] = stage
        entry["error"] = str(error_msg)[:5000]

        if touch_last_attempt:
            entry["last_attempt"] = now_iso()

    if book_id is not None:
        entry["book_id"] = int(book_id)

    if converted_path:
        entry["converted_path"] = str(
            converted_path
        )

    elif stage in {
        STAGE_ADD_ORIGINAL,
        STAGE_CONVERT,
        STAGE_FINALIZE_NO_CONVERSION,
    }:
        entry.pop(
            "converted_path",
            None,
        )

    save_retry_queue(
        queue_data,
        config,
    )

    print(
        f"[RETRY] Checkpoint {filepath.name}: "
        f"stage={stage}, "
        f"attempt="
        f"{entry.get('attempts', 1)}/"
        f"{entry.get('max_attempts', configured_max)}"
    )

    return entry


# ============================================================
# SHUTDOWN
# ============================================================

def signal_handler(
    signum,
    frame,
):
    print(
        "\n[SHUTDOWN] Received termination signal..."
    )

    write_ingest_status(
        "stopped",
        "",
        "shutdown requested",
    )

    raise SystemExit(0)


signal.signal(
    signal.SIGTERM,
    signal_handler,
)

signal.signal(
    signal.SIGINT,
    signal_handler,
)


# ============================================================
# FORMAT / TIMEOUT HELPERS
# ============================================================

def parse_format_list(formats_str):
    if not formats_str:
        return set()

    if isinstance(
        formats_str,
        (list, tuple, set),
    ):
        values = formats_str
    else:
        values = str(formats_str).split(",")

    result = set()

    for fmt in values:
        fmt = str(fmt).strip().lower()

        if not fmt:
            continue

        if not fmt.startswith("."):
            fmt = f".{fmt}"

        result.add(fmt)

    return result


def get_processing_timeout(settings):
    timeout_minutes = safe_int(
        settings.get(
            "ingest_timeout_minutes",
            15,
        ),
        15,
        minimum=1,
    )

    return timeout_minutes * 60


def make_deadline(settings):
    return (
        time.monotonic()
        + get_processing_timeout(settings)
    )


def remaining_seconds(deadline):
    remaining = (
        deadline
        - time.monotonic()
    )

    if remaining <= 0:
        raise TimeoutError(
            "overall ingest timeout exceeded"
        )

    return max(
        1,
        int(remaining),
    )


# ============================================================
# STALE TEMP CLEANUP
# ============================================================

def cleanup_stale_temps(
    tmp_dir,
    stale_minutes,
    settings=None,
):
    if not tmp_dir:
        return 0

    tmp_path = Path(tmp_dir)

    if not tmp_path.exists():
        return 0

    stale_minutes = safe_int(
        stale_minutes,
        120,
        minimum=1,
    )

    cutoff_timestamp = (
        time.time()
        - (stale_minutes * 60)
    )

    cleaned = 0

    try:
        children = list(
            tmp_path.iterdir()
        )

    except OSError as e:
        print(
            f"[CLEANUP] Error scanning "
            f"{tmp_path}: {e}"
        )

        return 0

    for child in children:
        try:
            if (
                child.stat().st_mtime
                >= cutoff_timestamp
            ):
                continue

            if child.is_dir():
                shutil.rmtree(child)

                print(
                    "[CLEANUP] Removed stale "
                    f"temp directory: {child.name}"
                )

            else:
                child.unlink()

                print(
                    "[CLEANUP] Removed stale "
                    f"temp file: {child.name}"
                )

            cleaned += 1

        except FileNotFoundError:
            continue

        except OSError as e:
            print(
                f"[CLEANUP] Error cleaning "
                f"{child}: {e}"
            )

    return cleaned


# ============================================================
# FILESYSTEM HELPERS
# ============================================================

def unique_destination(
    folder,
    filename,
):
    folder = Path(folder)

    destination = (
        folder
        / filename
    )

    if not destination.exists():
        return destination

    source_name = Path(filename)

    timestamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    return folder / (
        f"{source_name.stem}."
        f"{timestamp}."
        f"{uuid.uuid4().hex[:6]}"
        f"{source_name.suffix}"
    )


def move_to_folder(
    filepath,
    folder,
    overwrite=False,
):
    filepath = Path(filepath)
    folder = Path(folder)

    try:
        ensure_directory(folder)

        destination = (
            folder
            / filepath.name
        )

        if destination.exists():
            if overwrite:
                if destination.is_dir():
                    shutil.rmtree(
                        destination
                    )
                else:
                    destination.unlink()

            else:
                destination = unique_destination(
                    folder,
                    filepath.name,
                )

        result = shutil.move(
            str(filepath),
            str(destination),
        )

        return Path(result)

    except (
        OSError,
        shutil.Error,
    ) as e:
        print(
            f"[FILES] Error moving "
            f"{filepath} -> {folder}: {e}"
        )

        return None


def move_failed_file(
    filepath,
    config,
):
    filepath = Path(filepath)

    if not filepath.exists():
        return True

    destination = move_to_folder(
        filepath,
        config.get("failed_folder"),
    )

    if destination:
        print(
            f"[FAILED] Moved to: "
            f"{destination}"
        )

        return True

    return False


def cleanup_work_dir(path):
    if not path:
        return

    path = Path(path)

    try:
        if path.exists():
            shutil.rmtree(path)

    except OSError:
        # Stale-temp cleaner will get it later.
        pass


# ============================================================
# INGEST FILTERS
# ============================================================

def should_ingest(
    filepath,
    settings,
):
    ignored = parse_format_list(
        settings.get(
            "auto_ingest_ignored_formats",
            "",
        )
    )

    if (
        filepath.suffix.lower()
        in ignored
    ):
        return (
            False,
            f"format {filepath.suffix} "
            "is in ingest ignore list",
        )

    return True, "allowed"


def conversion_requested(
    source_ext,
    settings,
):
    """
    Determine whether automatic target-format enforcement is active.

    NOTE:
    auto_convert_retained_formats intentionally does NOT suppress
    conversion. "Retained" means retain the format, not "don't create
    the target".
    """
    if not safe_bool(
        settings.get(
            "auto_convert",
            1,
        ),
        True,
    ):
        return (
            False,
            "auto_convert disabled",
        )

    source_ext = str(
        source_ext
    ).strip().lower()

    if not source_ext.startswith("."):
        source_ext = (
            f".{source_ext}"
        )

    ignored = parse_format_list(
        settings.get(
            "auto_convert_ignored_formats",
            "",
        )
    )

    if source_ext in ignored:
        return (
            False,
            f"{source_ext} is ignored "
            "for conversion",
        )

    return (
        True,
        "target format should be ensured",
    )


# ============================================================
# BOOK / METADATA HELPERS
# ============================================================

def filename_title(filepath):
    title = Path(filepath).stem

    if " - " in title:
        title = title.split(
            " - ",
            1,
        )[0]

    return title.strip()


def extract_book_id_from_calibredb_output(
    output,
):
    """
    Handle common CWA/Calibre output forms such as:

        Added book ids: 42
        Added book id: 42
        Merged book ids: 42
        Updated book ids: 42
    """
    if not output:
        return None

    match = re.search(
        r"(?:Added|Merged|Updated)"
        r"\s+book\s+ids?"
        r"\s*:\s*([0-9,\s]+)",
        output,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    for value in (
        match.group(1).split(",")
    ):
        value = value.strip()

        if value.isdigit():
            return int(value)

    return None


def find_book_by_exact_title(
    filepath,
    config,
):
    """
    Conservative fallback only.

    Returns a book ID only if there is exactly ONE exact,
    case-insensitive title match.
    """
    library_root = Path(
        config.get(
            "calibre_library_dir"
        )
    )

    metadata_db = (
        library_root
        / "metadata.db"
    )

    if not metadata_db.exists():
        print(
            "[LIBRARY] Cannot find "
            f"metadata.db at {metadata_db}"
        )

        return None

    title = filename_title(
        filepath
    )

    if not title:
        return None

    conn = None

    try:
        conn = sqlite3.connect(
            str(metadata_db),
            timeout=30,
        )

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, title
            FROM books
            WHERE LOWER(TRIM(title)) =
                  LOWER(TRIM(?))
            ORDER BY id
            """,
            (title,),
        )

        rows = cursor.fetchall()

        if len(rows) == 1:
            book_id = int(
                rows[0][0]
            )

            print(
                "[LIBRARY] Found unique "
                f"exact-title book_id={book_id} "
                f"for '{title}'"
            )

            return book_id

        if len(rows) > 1:
            print(
                "[LIBRARY] Refusing ambiguous "
                f"title lookup for '{title}': "
                f"{len(rows)} exact matches"
            )

        else:
            print(
                "[LIBRARY] No exact title "
                f"match for '{title}'"
            )

        return None

    except sqlite3.Error as e:
        print(
            "[LIBRARY] Error searching "
            f"metadata.db: {e}"
        )

        return None

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# CALIBRE FORMAT DISCOVERY
# ============================================================

def get_book_format_paths(
    book_id,
    config,
):
    """
    Return actual library format files attached to book_id.

    Example:
        {
            "mobi": Path(...),
            "azw3": Path(...),
            "pdf": Path(...),
        }
    """
    library_root = Path(
        config.get(
            "calibre_library_dir"
        )
    )

    metadata_db = (
        library_root
        / "metadata.db"
    )

    if not metadata_db.exists():
        print(
            "[LIBRARY] ✗ Cannot inspect existing "
            f"formats; metadata.db missing: {metadata_db}"
        )

        return {}

    conn = None

    try:
        conn = sqlite3.connect(
            str(metadata_db),
            timeout=30,
        )

        cursor = conn.cursor()

        try:
            cursor.execute(
                "PRAGMA query_only = ON"
            )
        except sqlite3.Error:
            pass

        cursor.execute(
            """
            SELECT
                books.path,
                data.name,
                data.format
            FROM books
            JOIN data
                ON data.book = books.id
            WHERE books.id = ?
            ORDER BY data.format
            """,
            (int(book_id),),
        )

        formats = {}

        for (
            relative_dir,
            basename,
            fmt,
        ) in cursor.fetchall():

            if (
                not relative_dir
                or not basename
                or not fmt
            ):
                continue

            fmt = (
                str(fmt)
                .strip()
                .lower()
            )

            book_dir = (
                library_root
                / relative_dir
            )

            candidate = (
                book_dir
                / f"{basename}.{fmt}"
            )

            if not candidate.exists():
                # Calibre normally stores extensions in lower/upper
                # predictable form, but tolerate casing differences.
                try:
                    for possible in (
                        book_dir.iterdir()
                    ):
                        if (
                            possible.is_file()
                            and possible.stem
                            == basename
                            and possible.suffix.lower()
                            == f".{fmt}"
                        ):
                            candidate = possible
                            break

                except OSError:
                    pass

            if candidate.exists():
                formats[fmt] = (
                    candidate
                )

            else:
                print(
                    "[LIBRARY] WARN: metadata.db "
                    f"lists {fmt.upper()} for "
                    f"book {book_id}, but file "
                    f"was not found: {candidate}"
                )

        return formats

    except sqlite3.Error as e:
        print(
            "[LIBRARY] ✗ Error retrieving "
            f"formats for book {book_id}: {e}"
        )

        return {}

    finally:
        if conn is not None:
            conn.close()


# ============================================================
# CALIBRE IMPORT
# ============================================================

def add_to_library(
    filepath,
    automerge_mode=None,
    timeout_sec=900,
    config=None,
):
    """
    Add incoming/original format to Calibre.

    automerge_mode comes directly from cwa_settings after validation.
    """
    filepath = Path(filepath)

    if not CALIBRE_LIBRARY:
        print(
            "[LIBRARY] ✗ Calibre library "
            "path is not configured"
        )

        return False, None

    if not CALIBREDB:
        print(
            "[LIBRARY] ✗ calibredb "
            "is not available"
        )

        return False, None

    cmd = [
        CALIBREDB,
        "add",
        "--with-library",
        CALIBRE_LIBRARY,
    ]

    if automerge_mode:
        cmd.extend([
            "--automerge",
            automerge_mode,
        ])

    cmd.append(
        str(filepath)
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(
                1,
                int(timeout_sec),
            ),
        )

        stdout = (
            result.stdout
            or ""
        )

        stderr = (
            result.stderr
            or ""
        )

        combined = (
            f"{stdout}\n{stderr}"
        ).strip()

        print(
            "[LIBRARY] STDOUT: "
            f"{stdout.strip() or '(none)'}"
        )

        print(
            "[LIBRARY] STDERR: "
            f"{stderr.strip() or '(none)'}"
        )

        if result.returncode != 0:
            error = (
                stderr
                or stdout
                or "calibredb add failed"
            ).strip()

            print(
                f"[LIBRARY] ✗ {filepath.name}: "
                f"{error[:5000]}"
            )

            return False, None

        book_id = (
            extract_book_id_from_calibredb_output(
                combined
            )
        )

        if book_id is not None:
            print(
                "[LIBRARY] ✓ Added/merged "
                f"into book_id={book_id}"
            )

            return True, book_id

        # Automerge can succeed without output that our parser
        # recognizes. Use the cautious fallback.
        if (
            automerge_mode
            and config is not None
        ):
            book_id = (
                find_book_by_exact_title(
                    filepath,
                    config,
                )
            )

            if book_id is not None:
                return True, book_id

        print(
            "[LIBRARY] Add succeeded, but "
            "no unambiguous book ID could "
            "be determined"
        )

        return True, None

    except subprocess.TimeoutExpired:
        print(
            "[LIBRARY] ⏱ TIMEOUT adding "
            f"{filepath.name}"
        )

        return False, None

    except OSError as e:
        print(
            f"[LIBRARY] ✗ {filepath.name}: {e}"
        )

        return False, None


def add_format_to_book(
    book_id,
    filepath,
    timeout_sec,
):
    """
    Attach the generated target format to the existing book.
    """
    filepath = Path(filepath)

    if not CALIBREDB:
        print(
            "[LIBRARY] ✗ calibredb "
            "is not available"
        )

        return False

    if not filepath.exists():
        print(
            "[LIBRARY] ✗ Converted "
            f"file missing: {filepath}"
        )

        return False

    cmd = [
        CALIBREDB,
        "add_format",
        "--with-library",
        CALIBRE_LIBRARY,
        str(book_id),
        str(filepath),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(
                1,
                int(timeout_sec),
            ),
        )

        if result.returncode == 0:
            print(
                "[LIBRARY] ✓ Format "
                "attached/replaced on "
                f"book {book_id}: "
                f"{filepath.name}"
            )

            return True

        error = (
            result.stderr
            or result.stdout
            or "calibredb add_format failed"
        ).strip()

        print(
            f"[LIBRARY] ✗ {filepath.name}: "
            f"{error[:5000]}"
        )

        return False

    except subprocess.TimeoutExpired:
        print(
            "[LIBRARY] ⏱ TIMEOUT "
            f"adding format to book "
            f"{book_id}"
        )

        return False

    except OSError as e:
        print(
            f"[LIBRARY] ✗ {filepath.name}: {e}"
        )

        return False


# ============================================================
# CONVERSION SOURCE SELECTION
# ============================================================

def get_conversion_candidates(
    book_id,
    incoming_filepath,
    target_format,
    settings,
    config,
):
    """
    Inspect ALL formats attached to the resulting Calibre book.

    Returns:
        target_already_exists: bool
        candidates: list[Path]
    """
    target_format = (
        str(target_format)
        .strip()
        .lower()
        .lstrip(".")
    )

    formats = get_book_format_paths(
        book_id,
        config,
    )

    if formats:
        print(
            f"[CONVERT] Book {book_id} "
            "currently has: "
            + ", ".join(
                fmt.upper()
                for fmt in sorted(formats)
            )
        )

    else:
        print(
            f"[CONVERT] Book {book_id} "
            "has no readable existing "
            "format files"
        )

    # Target already exists: nothing to do.
    if target_format in formats:
        print(
            f"[CONVERT] ✓ Book {book_id} "
            f"already has "
            f"{target_format.upper()}; "
            "conversion skipped"
        )

        return True, []

    ignored_sources = parse_format_list(
        settings.get(
            "auto_convert_ignored_formats",
            "",
        )
    )

    candidates_by_format = {}

    # Existing Calibre formats.
    for fmt, path in (
        formats.items()
    ):
        fmt = fmt.lower()

        if fmt == target_format:
            continue

        if (
            f".{fmt}"
            in ignored_sources
        ):
            print(
                "[CONVERT] Existing "
                f"{fmt.upper()} ignored "
                "by auto_convert_ignored_formats"
            )

            continue

        if (
            fmt in SUPPORTED_BOOK_FORMATS
            and path.exists()
        ):
            candidates_by_format[
                fmt
            ] = path

    # Incoming path as fallback if it is not somehow represented
    # in metadata.db yet.
    incoming_filepath = Path(
        incoming_filepath
    )

    if incoming_filepath.exists():
        incoming_fmt = (
            incoming_filepath
            .suffix
            .lower()
            .lstrip(".")
        )

        if (
            incoming_fmt
            and incoming_fmt
            != target_format
            and incoming_fmt
            in SUPPORTED_BOOK_FORMATS
            and f".{incoming_fmt}"
            not in ignored_sources
            and incoming_fmt
            not in candidates_by_format
        ):
            candidates_by_format[
                incoming_fmt
            ] = incoming_filepath

    ordered = []

    for fmt in (
        CONVERSION_SOURCE_PRIORITY
    ):
        path = candidates_by_format.pop(
            fmt,
            None,
        )

        if path is not None:
            ordered.append(path)

    # Future/new supported formats can still be tried after known ones.
    for fmt in sorted(
        candidates_by_format
    ):
        ordered.append(
            candidates_by_format[
                fmt
            ]
        )

    if ordered:
        print(
            "[CONVERT] Candidate sources "
            f"for {target_format.upper()}:"
        )

        for index, candidate in enumerate(
            ordered,
            start=1,
        ):
            print(
                f"[CONVERT]   {index}. "
                f"{candidate.suffix.lstrip('.').upper()} "
                f"→ {candidate}"
            )

    else:
        print(
            "[CONVERT] No eligible source "
            f"format is available for "
            f"{target_format.upper()}"
        )

    return False, ordered


# ============================================================
# CONVERSION
# ============================================================

def run_ebook_convert(
    source_path,
    target_format,
    tmp_root,
    timeout_sec,
):
    """
    Attempt one source format -> target conversion.

    Returns:
        (True, Path, "")
        (False, None, error_message)
    """
    source_path = Path(
        source_path
    )

    target_format = (
        str(target_format)
        .strip()
        .lower()
        .lstrip(".")
    )

    if not EBOOK_CONVERT:
        return (
            False,
            None,
            "ebook-convert not available",
        )

    try:
        ensure_directory(
            tmp_root
        )

        work_dir = Path(
            tempfile.mkdtemp(
                prefix="ingest-",
                dir=str(tmp_root),
            )
        )

    except OSError as e:
        return (
            False,
            None,
            f"cannot create conversion "
            f"work directory: {e}",
        )

    env = os.environ.copy()

    env["TEMP"] = str(
        work_dir
    )

    env["TMP"] = str(
        work_dir
    )

    env["TMPDIR"] = str(
        work_dir
    )

    # --------------------------------------------------------
    # KEPUB special case
    # --------------------------------------------------------

    if target_format == "kepub":
        if not KEPUBIFY:
            cleanup_work_dir(
                work_dir
            )

            return (
                False,
                None,
                "target is KEPUB but kepubify "
                "is not available",
            )

        intermediate_epub = (
            work_dir
            / f"{source_path.stem}.epub"
        )

        try:
            if (
                source_path.suffix.lower()
                == ".epub"
            ):
                shutil.copy2(
                    source_path,
                    intermediate_epub,
                )

            else:
                result = subprocess.run(
                    [
                        EBOOK_CONVERT,
                        str(source_path),
                        str(intermediate_epub),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=max(
                        1,
                        int(timeout_sec),
                    ),
                )

                if (
                    result.returncode != 0
                    or not intermediate_epub.exists()
                ):
                    error = (
                        result.stderr
                        or result.stdout
                        or "ebook-convert failed "
                           "while creating EPUB "
                           "for KEPUB conversion"
                    ).strip()

                    cleanup_work_dir(
                        work_dir
                    )

                    return (
                        False,
                        None,
                        error[:5000],
                    )

            # Match CWA's kepubify approach:
            # convert EPUB into KEPUB inside the temporary directory.
            result = subprocess.run(
                [
                    KEPUBIFY,
                    "--inplace",
                    "--calibre",
                    "--output",
                    str(work_dir),
                    str(intermediate_epub),
                ],
                capture_output=True,
                text=True,
                timeout=max(
                    1,
                    int(timeout_sec),
                ),
            )

            if result.returncode != 0:
                error = (
                    result.stderr
                    or result.stdout
                    or "kepubify failed"
                ).strip()

                cleanup_work_dir(
                    work_dir
                )

                return (
                    False,
                    None,
                    error[:5000],
                )

            expected = (
                work_dir
                / f"{source_path.stem}.kepub"
            )

            if expected.exists():
                return (
                    True,
                    expected,
                    "",
                )

            # Be tolerant of differing kepubify output naming.
            kepub_candidates = list(
                work_dir.glob(
                    "*.kepub"
                )
            )

            if not kepub_candidates:
                kepub_candidates = list(
                    work_dir.glob(
                        "*.kepub.epub"
                    )
                )

            if kepub_candidates:
                return (
                    True,
                    kepub_candidates[0],
                    "",
                )

            cleanup_work_dir(
                work_dir
            )

            return (
                False,
                None,
                "kepubify completed but no "
                "KEPUB output was found",
            )

        except subprocess.TimeoutExpired:
            cleanup_work_dir(
                work_dir
            )

            return (
                False,
                None,
                "KEPUB conversion timed out",
            )

        except OSError as e:
            cleanup_work_dir(
                work_dir
            )

            return (
                False,
                None,
                str(e),
            )

    # --------------------------------------------------------
    # Normal ebook-convert path
    # --------------------------------------------------------

    output_path = (
        work_dir
        / f"{source_path.stem}."
          f"{target_format}"
    )

    cmd = [
        EBOOK_CONVERT,
        str(source_path),
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=max(
                1,
                int(timeout_sec),
            ),
        )

        if (
            result.returncode == 0
            and output_path.exists()
            and output_path.is_file()
        ):
            return (
                True,
                output_path,
                "",
            )

        error = (
            result.stderr
            or result.stdout
            or "ebook-convert failed"
        ).strip()

        cleanup_work_dir(
            work_dir
        )

        return (
            False,
            None,
            error[:5000],
        )

    except subprocess.TimeoutExpired:
        cleanup_work_dir(
            work_dir
        )

        return (
            False,
            None,
            "conversion timed out",
        )

    except OSError as e:
        cleanup_work_dir(
            work_dir
        )

        return (
            False,
            None,
            str(e),
        )


def convert_best_available_format(
    book_id,
    incoming_filepath,
    target_format,
    settings,
    config,
    deadline,
):
    """
    Try every appropriate format already attached to book_id.

    Returns:
        ("already_present", None)
        ("success", Path)
        ("skipped", None)

    "skipped" is intentional and NOT a retry failure. It means no
    available source format could successfully produce the configured
    target format.
    """
    (
        target_exists,
        candidates,
    ) = get_conversion_candidates(
        book_id,
        incoming_filepath,
        target_format,
        settings,
        config,
    )

    if target_exists:
        return (
            "already_present",
            None,
        )

    if not candidates:
        print(
            "[CONVERT] SKIP: None of the "
            f"available formats for book "
            f"{book_id} can be used to "
            f"create configured target "
            f"{target_format.upper()}."
        )

        return (
            "skipped",
            None,
        )

    failures = []

    for source_path in candidates:
        source_format = (
            source_path
            .suffix
            .lower()
            .lstrip(".")
        )

        print(
            "[CONVERT] Trying "
            f"{source_format.upper()} → "
            f"{target_format.upper()} "
            f"for book {book_id}"
        )

        write_ingest_status(
            "processing",
            incoming_filepath.name,
            (
                f"trying "
                f"{source_format} -> "
                f"{target_format}"
            ),
        )

        success, converted_path, error = (
            run_ebook_convert(
                source_path,
                target_format,
                config.get(
                    "tmp_conversion_dir"
                )
                or "/tmp/cwa_conversions",
                remaining_seconds(
                    deadline
                ),
            )
        )

        if success:
            print(
                "[CONVERT] ✓ Successfully "
                f"converted "
                f"{source_format.upper()} → "
                f"{target_format.upper()} "
                f"for book {book_id}"
            )

            return (
                "success",
                converted_path,
            )

        failures.append(
            (
                source_format,
                error,
            )
        )

        print(
            "[CONVERT] ✗ "
            f"{source_format.upper()} could "
            f"not be converted to "
            f"{target_format.upper()}: "
            f"{error[:1000]}"
        )

        print(
            "[CONVERT] Trying next "
            "available source format..."
        )

    # User-requested behavior:
    # all available sources failed => message and continue ingest.
    print(
        "[CONVERT] SKIP: None of the "
        f"available formats for book "
        f"{book_id} could be converted "
        f"to configured target "
        f"{target_format.upper()}."
    )

    if failures:
        summary = "; ".join(
            f"{fmt.upper()}: "
            f"{error[:250]}"
            for fmt, error in failures
        )

        print(
            "[CONVERT] Conversion failure "
            f"summary: {summary}"
        )

    print(
        "[CONVERT] Original/existing "
        "formats will be retained; "
        "ingest will continue without "
        f"{target_format.upper()}."
    )

    return (
        "skipped",
        None,
    )


# ============================================================
# FINALIZATION
# ============================================================

def finalize_no_conversion(
    original_filepath,
    config,
):
    original_filepath = Path(
        original_filepath
    )

    # It may already have been moved by a previous partial finalize.
    if not original_filepath.exists():
        print(
            "[FINALIZE] Source already "
            f"absent: {original_filepath}"
        )

        return True

    destination = move_to_folder(
        original_filepath,
        config.get(
            "processed_folder"
        ),
    )

    if destination is None:
        return False

    print(
        f"[INGEST] ✓ Imported: "
        f"{destination.name}"
    )

    return True


def finalize_with_conversion(
    original_filepath,
    converted_filepath,
    config,
):
    """
    Retry-safe finalization.

    Either file may already have been moved by an earlier partial attempt.
    """
    original_filepath = Path(
        original_filepath
    )

    converted_filepath = (
        Path(converted_filepath)
        if converted_filepath
        else None
    )

    processed_folder = Path(
        config.get(
            "processed_folder"
        )
    )

    ensure_directory(
        processed_folder
    )

    moved_names = []

    if (
        converted_filepath
        and converted_filepath.exists()
    ):
        converted_destination = (
            move_to_folder(
                converted_filepath,
                processed_folder,
            )
        )

        if converted_destination is None:
            return False

        moved_names.append(
            converted_destination.name
        )

        cleanup_work_dir(
            converted_filepath.parent
        )

    if original_filepath.exists():
        original_destination = (
            move_to_folder(
                original_filepath,
                processed_folder,
            )
        )

        if original_destination is None:
            return False

        moved_names.append(
            original_destination.name
        )

    if moved_names:
        print(
            "[INGEST] ✓ Finalized: "
            + " + ".join(
                moved_names
            )
        )

    else:
        print(
            "[INGEST] ✓ Finalization "
            "already completed"
        )

    return True


# ============================================================
# RETRY / FAILURE HELPERS
# ============================================================

def queue_pipeline_failure(
    original_filepath,
    error,
    config,
    stage,
    book_id=None,
    converted_path=None,
    retry_entry=None,
):
    attempts = None

    if retry_entry:
        attempts = safe_int(
            retry_entry.get(
                "attempts",
                1,
            ),
            1,
            minimum=1,
        )

    upsert_retry_entry(
        original_filepath,
        error,
        config,
        stage=stage,
        book_id=book_id,
        converted_path=converted_path,
        attempts=attempts,
        touch_last_attempt=True,
    )

    write_ingest_status(
        "idle",
        "",
        "file queued for retry",
    )


def permanent_failure(
    original_filepath,
    detail,
    config,
):
    original_filepath = Path(
        original_filepath
    )

    moved = move_failed_file(
        original_filepath,
        config,
    )

    if moved:
        remove_retry_entry(
            original_filepath.name,
            config,
        )

        write_ingest_status(
            "failed",
            original_filepath.name,
            detail,
        )

        return False

    # Keep it queued if failed-folder move failed so the normal scanner
    # cannot immediately start processing it again.
    upsert_retry_entry(
        original_filepath,
        (
            f"{detail}; unable to move "
            "source to failed folder"
        ),
        config,
        stage=STAGE_FINALIZE,
    )

    write_ingest_status(
        "failed",
        original_filepath.name,
        (
            f"{detail}; failed-folder "
            "move failed"
        ),
    )

    return False


# ============================================================
# MAIN BOOK PIPELINE
# ============================================================

def process_book(
    filepath,
    settings,
    config,
    retry_entry=None,
):
    """
    Process or resume a single ingest item.
    """
    filepath = Path(filepath)
    original_filepath = filepath

    retry_stage = (
        retry_entry.get("stage")
        if retry_entry
        else None
    )

    # A finalization retry may legitimately find that the source was
    # already moved before the previous process crashed.
    source_required = (
        retry_stage
        not in {
            STAGE_FINALIZE,
            STAGE_FINALIZE_NO_CONVERSION,
        }
    )

    if (
        source_required
        and not original_filepath.exists()
    ):
        print(
            "[INGEST] Source no longer "
            f"exists: {original_filepath}"
        )

        remove_retry_entry(
            original_filepath.name,
            config,
        )

        return False

    # --------------------------------------------------------
    # Validate actual source when present
    # --------------------------------------------------------

    if original_filepath.exists():
        try:
            if (
                original_filepath.stat().st_size
                == 0
            ):
                print(
                    f"[INGEST] ✗ "
                    f"{original_filepath.name} "
                    "is empty"
                )

                return permanent_failure(
                    original_filepath,
                    "empty file",
                    config,
                )

        except OSError as e:
            queue_pipeline_failure(
                original_filepath,
                f"cannot stat source: {e}",
                config,
                STAGE_ADD_ORIGINAL,
                retry_entry=retry_entry,
            )

            return False

        allowed, reason = should_ingest(
            original_filepath,
            settings,
        )

        if not allowed:
            print(
                f"[INGEST] Skip "
                f"{original_filepath.name}: "
                f"{reason}"
            )

            remove_retry_entry(
                original_filepath.name,
                config,
            )

            return False

    source_ext = (
        original_filepath
        .suffix
        .lower()
    )

    convert_requested, conversion_reason = (
        conversion_requested(
            source_ext,
            settings,
        )
    )

    target_format = get_target_format(
        settings
    )

    automerge_mode = (
        get_automerge_mode(
            settings
        )
    )

    print(
        "[INGEST] Conversion policy for "
        f"{original_filepath.name}: "
        f"{conversion_reason}; "
        f"target="
        f"{target_format.upper()}"
    )

    print(
        "[INGEST] CWA automerge mode: "
        f"{automerge_mode or 'disabled'}"
    )

    deadline = make_deadline(
        settings
    )

    stage = (
        retry_stage
        or STAGE_ADD_ORIGINAL
    )

    book_id = None
    converted_filepath = None

    if retry_entry:
        raw_book_id = (
            retry_entry.get(
                "book_id"
            )
        )

        if raw_book_id is not None:
            try:
                book_id = int(
                    raw_book_id
                )

            except (
                TypeError,
                ValueError,
            ):
                book_id = None

        raw_converted = (
            retry_entry.get(
                "converted_path"
            )
        )

        if raw_converted:
            converted_filepath = Path(
                raw_converted
            )

        print(
            f"[RETRY] Resuming "
            f"{original_filepath.name}: "
            f"stage={stage}, "
            f"book_id={book_id}, "
            f"converted="
            f"{converted_filepath}"
        )

    try:
        # ====================================================
        # ADD ORIGINAL
        # ====================================================

        if stage == STAGE_ADD_ORIGINAL:
            write_ingest_status(
                "processing",
                original_filepath.name,
                "adding original format",
            )

            (
                success,
                book_id,
            ) = add_to_library(
                original_filepath,
                automerge_mode=automerge_mode,
                timeout_sec=remaining_seconds(
                    deadline
                ),
                config=config,
            )

            if not success:
                queue_pipeline_failure(
                    original_filepath,
                    "failed to add original format",
                    config,
                    STAGE_ADD_ORIGINAL,
                    retry_entry=retry_entry,
                )

                return False

            if (
                convert_requested
                and book_id is None
            ):
                # We cannot safely inspect existing formats or attach
                # target output without knowing the logical record.
                queue_pipeline_failure(
                    original_filepath,
                    (
                        "original added but "
                        "book ID could not be "
                        "determined"
                    ),
                    config,
                    STAGE_ADD_ORIGINAL,
                    retry_entry=retry_entry,
                )

                return False

            if (
                convert_requested
                and book_id is not None
            ):
                stage = STAGE_CONVERT

            else:
                stage = (
                    STAGE_FINALIZE_NO_CONVERSION
                )

            upsert_retry_entry(
                original_filepath,
                "pipeline checkpoint",
                config,
                stage=stage,
                book_id=book_id,
                attempts=(
                    safe_int(
                        retry_entry.get(
                            "attempts",
                            1,
                        ),
                        1,
                        minimum=1,
                    )
                    if retry_entry
                    else 1
                ),
                touch_last_attempt=False,
            )

        # ====================================================
        # CONVERSION
        # ====================================================

        if stage == STAGE_CONVERT:
            if not book_id:
                queue_pipeline_failure(
                    original_filepath,
                    (
                        "cannot inspect existing "
                        "formats without book ID"
                    ),
                    config,
                    STAGE_ADD_ORIGINAL,
                    retry_entry=retry_entry,
                )

                return False

            (
                conversion_status,
                converted_filepath,
            ) = convert_best_available_format(
                book_id,
                original_filepath,
                target_format,
                settings,
                config,
                deadline,
            )

            # Target is already attached to the book.
            if (
                conversion_status
                == "already_present"
            ):
                stage = (
                    STAGE_FINALIZE_NO_CONVERSION
                )

                upsert_retry_entry(
                    original_filepath,
                    (
                        "configured target "
                        "already exists"
                    ),
                    config,
                    stage=stage,
                    book_id=book_id,
                    attempts=(
                        safe_int(
                            retry_entry.get(
                                "attempts",
                                1,
                            ),
                            1,
                            minimum=1,
                        )
                        if retry_entry
                        else 1
                    ),
                    touch_last_attempt=False,
                )

            # User-requested behavior:
            # conversion impossible/failed across all candidates is NOT
            # a retryable ingest failure.
            elif (
                conversion_status
                == "skipped"
            ):
                print(
                    "[INGEST] Continuing "
                    "without target conversion "
                    f"{target_format.upper()}."
                )

                write_ingest_status(
                    "processing",
                    original_filepath.name,
                    (
                        "no available format "
                        "could be converted to "
                        f"{target_format}; "
                        "conversion skipped"
                    ),
                )

                stage = (
                    STAGE_FINALIZE_NO_CONVERSION
                )

                upsert_retry_entry(
                    original_filepath,
                    (
                        "conversion skipped; "
                        "no usable source format"
                    ),
                    config,
                    stage=stage,
                    book_id=book_id,
                    attempts=(
                        safe_int(
                            retry_entry.get(
                                "attempts",
                                1,
                            ),
                            1,
                            minimum=1,
                        )
                        if retry_entry
                        else 1
                    ),
                    touch_last_attempt=False,
                )

            elif (
                conversion_status
                == "success"
                and converted_filepath
                is not None
            ):
                stage = (
                    STAGE_ATTACH_FORMAT
                )

                upsert_retry_entry(
                    original_filepath,
                    "conversion complete",
                    config,
                    stage=stage,
                    book_id=book_id,
                    converted_path=(
                        converted_filepath
                    ),
                    attempts=(
                        safe_int(
                            retry_entry.get(
                                "attempts",
                                1,
                            ),
                            1,
                            minimum=1,
                        )
                        if retry_entry
                        else 1
                    ),
                    touch_last_attempt=False,
                )

            else:
                # Defensive fallback. convert_best_available_format()
                # should normally return only the documented states.
                print(
                    "[CONVERT] Unexpected "
                    "conversion state; skipping "
                    "target conversion."
                )

                stage = (
                    STAGE_FINALIZE_NO_CONVERSION
                )

        # ====================================================
        # ATTACH CONVERTED TARGET
        # ====================================================

        if stage == STAGE_ATTACH_FORMAT:
            if not book_id:
                queue_pipeline_failure(
                    original_filepath,
                    (
                        "missing book ID for "
                        "converted format"
                    ),
                    config,
                    STAGE_ADD_ORIGINAL,
                    retry_entry=retry_entry,
                )

                return False

            # Temp cleanup or restart may have removed the converted file.
            # Re-select from existing Calibre formats without re-adding
            # the original.
            if (
                converted_filepath is None
                or not converted_filepath.exists()
            ):
                print(
                    "[INGEST] Converted temp "
                    "file is missing; selecting "
                    "the best source from the "
                    "book's existing formats again."
                )

                (
                    conversion_status,
                    converted_filepath,
                ) = convert_best_available_format(
                    book_id,
                    original_filepath,
                    target_format,
                    settings,
                    config,
                    deadline,
                )

                if (
                    conversion_status
                    == "already_present"
                ):
                    print(
                        "[INGEST] Target "
                        f"{target_format.upper()} "
                        "is already attached; "
                        "continuing to finalization."
                    )

                    stage = (
                        STAGE_FINALIZE_NO_CONVERSION
                    )

                elif (
                    conversion_status
                    == "skipped"
                ):
                    print(
                        "[INGEST] No available "
                        "format could be converted "
                        f"to {target_format.upper()}; "
                        "continuing without target "
                        "conversion."
                    )

                    stage = (
                        STAGE_FINALIZE_NO_CONVERSION
                    )

                elif (
                    conversion_status
                    == "success"
                    and converted_filepath
                    is not None
                ):
                    upsert_retry_entry(
                        original_filepath,
                        "conversion recreated",
                        config,
                        stage=(
                            STAGE_ATTACH_FORMAT
                        ),
                        book_id=book_id,
                        converted_path=(
                            converted_filepath
                        ),
                        attempts=(
                            safe_int(
                                retry_entry.get(
                                    "attempts",
                                    1,
                                ),
                                1,
                                minimum=1,
                            )
                            if retry_entry
                            else 1
                        ),
                        touch_last_attempt=False,
                    )

                else:
                    # Same clean-skip semantics.
                    stage = (
                        STAGE_FINALIZE_NO_CONVERSION
                    )

            if stage == STAGE_ATTACH_FORMAT:
                write_ingest_status(
                    "processing",
                    original_filepath.name,
                    (
                        "attaching converted "
                        f"{target_format} format"
                    ),
                )

                if not add_format_to_book(
                    book_id,
                    converted_filepath,
                    remaining_seconds(
                        deadline
                    ),
                ):
                    # This IS retryable. We successfully created a target
                    # file but failed to commit it to Calibre.
                    queue_pipeline_failure(
                        original_filepath,
                        (
                            "failed to attach "
                            "converted target format"
                        ),
                        config,
                        STAGE_ATTACH_FORMAT,
                        book_id=book_id,
                        converted_path=(
                            converted_filepath
                        ),
                        retry_entry=retry_entry,
                    )

                    return False

                stage = STAGE_FINALIZE

                upsert_retry_entry(
                    original_filepath,
                    (
                        "target attached; "
                        "awaiting finalization"
                    ),
                    config,
                    stage=stage,
                    book_id=book_id,
                    converted_path=(
                        converted_filepath
                    ),
                    attempts=(
                        safe_int(
                            retry_entry.get(
                                "attempts",
                                1,
                            ),
                            1,
                            minimum=1,
                        )
                        if retry_entry
                        else 1
                    ),
                    touch_last_attempt=False,
                )

        # ====================================================
        # FINALIZE WITHOUT NEW TARGET FILE
        # ====================================================

        if (
            stage
            == STAGE_FINALIZE_NO_CONVERSION
        ):
            write_ingest_status(
                "processing",
                original_filepath.name,
                "finalizing import",
            )

            if not finalize_no_conversion(
                original_filepath,
                config,
            ):
                queue_pipeline_failure(
                    original_filepath,
                    (
                        "import succeeded; "
                        "processed move failed"
                    ),
                    config,
                    STAGE_FINALIZE_NO_CONVERSION,
                    book_id=book_id,
                    retry_entry=retry_entry,
                )

                return False

            if book_id:
                write_metadata_change_log(
                    book_id=book_id,
                    title=filename_title(
                        original_filepath
                    ),
                    authors=[],
                    source="auto-ingest",
                    target_format=(
                        target_format
                    ),
                )

            remove_retry_entry(
                original_filepath.name,
                config,
            )

            write_ingest_status(
                "idle",
                "",
                "processing complete",
            )

            print(
                "[INGEST] ✓ Completed "
                f"{original_filepath.name} "
                f"(book_id={book_id})"
            )

            return True

        # ====================================================
        # FINALIZE ORIGINAL + GENERATED TARGET
        # ====================================================

        if stage == STAGE_FINALIZE:
            write_ingest_status(
                "processing",
                original_filepath.name,
                "finalizing import",
            )

            if not finalize_with_conversion(
                original_filepath,
                converted_filepath,
                config,
            ):
                queue_pipeline_failure(
                    original_filepath,
                    (
                        "formats imported; "
                        "processed move failed"
                    ),
                    config,
                    STAGE_FINALIZE,
                    book_id=book_id,
                    converted_path=(
                        converted_filepath
                    ),
                    retry_entry=retry_entry,
                )

                return False

            if book_id:
                write_metadata_change_log(
                    book_id=book_id,
                    title=filename_title(
                        original_filepath
                    ),
                    authors=[],
                    source="auto-ingest",
                    target_format=(
                        target_format
                    ),
                    conversion="success",
                )

            remove_retry_entry(
                original_filepath.name,
                config,
            )

            write_ingest_status(
                "idle",
                "",
                "processing complete",
            )

            print(
                "[INGEST] ✓ Completed "
                f"{original_filepath.name} "
                f"(book_id={book_id}, "
                f"target="
                f"{target_format.upper()})"
            )

            return True

        raise RuntimeError(
            f"Unknown pipeline stage: {stage}"
        )

    except TimeoutError as e:
        print(
            f"[INGEST] ⏱ "
            f"{original_filepath.name}: {e}"
        )

        # If timeout occurred while merely attempting target conversion,
        # the user requested clean skip when conversion cannot be done.
        if stage == STAGE_CONVERT:
            print(
                "[CONVERT] SKIP: Overall "
                "ingest deadline was reached "
                "while attempting target "
                "conversion. Continuing "
                "without target conversion."
            )

            if finalize_no_conversion(
                original_filepath,
                config,
            ):
                if book_id:
                    write_metadata_change_log(
                        book_id=book_id,
                        title=filename_title(
                            original_filepath
                        ),
                        authors=[],
                        source="auto-ingest",
                        target_format=(
                            target_format
                        ),
                        conversion=(
                            "skipped_timeout"
                        ),
                    )

                remove_retry_entry(
                    original_filepath.name,
                    config,
                )

                write_ingest_status(
                    "idle",
                    "",
                    (
                        "conversion timed out; "
                        "ingest completed without "
                        "target format"
                    ),
                )

                return True

        queue_pipeline_failure(
            original_filepath,
            str(e),
            config,
            stage,
            book_id=book_id,
            converted_path=(
                converted_filepath
            ),
            retry_entry=retry_entry,
        )

        return False

    except Exception as e:
        print(
            "[INGEST] Unexpected pipeline "
            f"error for "
            f"{original_filepath.name}: {e}"
        )

        queue_pipeline_failure(
            original_filepath,
            (
                "unexpected pipeline error: "
                f"{e}"
            ),
            config,
            stage,
            book_id=book_id,
            converted_path=(
                converted_filepath
            ),
            retry_entry=retry_entry,
        )

        return False


# ============================================================
# RETRY PROCESSING
# ============================================================

def process_from_retry_queue(
    settings,
    config,
):
    retry_interval = safe_int(
        config.get(
            "retry_interval_seconds",
            300,
        ),
        300,
        minimum=1,
    )

    queue_snapshot = load_retry_queue(
        config
    )

    successful = []

    for snapshot_entry in queue_snapshot:
        if not isinstance(
            snapshot_entry,
            dict,
        ):
            continue

        filename = snapshot_entry.get(
            "filename"
        )

        if not filename:
            continue

        entry = get_retry_entry(
            filename,
            config,
        )

        if not entry:
            continue

        try:
            last_attempt = (
                datetime.fromisoformat(
                    entry.get(
                        "last_attempt",
                        "",
                    )
                )
            )

        except (
            ValueError,
            TypeError,
        ):
            print(
                "[RETRY] Invalid "
                f"last_attempt for "
                f"{filename}; making "
                "eligible immediately"
            )

            last_attempt = datetime.min

        if (
            datetime.now()
            - last_attempt
            < timedelta(
                seconds=retry_interval
            )
        ):
            continue

        attempts = safe_int(
            entry.get(
                "attempts",
                1,
            ),
            1,
            minimum=1,
        )

        max_attempts = safe_int(
            entry.get(
                "max_attempts",
                config.get(
                    "max_retry_attempts",
                    3,
                ),
            ),
            3,
            minimum=1,
        )

        filepath = Path(
            entry.get(
                "original_path",
                "",
            )
        )

        stage = entry.get(
            "stage",
            STAGE_ADD_ORIGINAL,
        )

        # ----------------------------------------------------
        # Retry limit exhausted
        # ----------------------------------------------------

        if attempts >= max_attempts:
            print(
                "[RETRY] Max attempts "
                f"({max_attempts}) reached "
                f"for {filename}"
            )

            if filepath.exists():
                if move_failed_file(
                    filepath,
                    config,
                ):
                    remove_retry_entry(
                        filename,
                        config,
                    )

                    write_ingest_status(
                        "failed",
                        filename,
                        (
                            "maximum retry "
                            "attempts reached"
                        ),
                    )

                else:
                    # Keep it queued so normal scanning cannot pick
                    # the same source right back up.
                    live_queue = (
                        load_retry_queue(
                            config
                        )
                    )

                    live_entry = (
                        find_retry_entry(
                            live_queue,
                            filename,
                        )
                    )

                    if live_entry:
                        live_entry[
                            "last_attempt"
                        ] = now_iso()

                        live_entry[
                            "error"
                        ] = (
                            "maximum retries "
                            "reached; failed-folder "
                            "move failed"
                        )

                        save_retry_queue(
                            live_queue,
                            config,
                        )

                continue

            # For finalization stages, an absent source can be legitimate.
            if stage in {
                STAGE_FINALIZE,
                STAGE_FINALIZE_NO_CONVERSION,
            }:
                pass

            else:
                remove_retry_entry(
                    filename,
                    config,
                )

                continue

        # ----------------------------------------------------
        # Missing source
        # ----------------------------------------------------

        if (
            not filepath.exists()
            and stage
            not in {
                STAGE_FINALIZE,
                STAGE_FINALIZE_NO_CONVERSION,
            }
        ):
            print(
                "[RETRY] Source no longer "
                f"exists: {filename}"
            )

            remove_retry_entry(
                filename,
                config,
            )

            continue

        # ----------------------------------------------------
        # Next retry
        # ----------------------------------------------------

        next_attempt = (
            attempts + 1
        )

        live_queue = load_retry_queue(
            config
        )

        live_entry = find_retry_entry(
            live_queue,
            filename,
        )

        if not live_entry:
            continue

        live_entry[
            "attempts"
        ] = next_attempt

        live_entry[
            "max_attempts"
        ] = max_attempts

        live_entry[
            "last_attempt"
        ] = now_iso()

        save_retry_queue(
            live_queue,
            config,
        )

        retry_entry = get_retry_entry(
            filename,
            config,
        )

        print(
            f"[RETRY] Attempt "
            f"{next_attempt}/"
            f"{max_attempts} "
            f"for {filename} "
            f"(stage="
            f"{retry_entry.get('stage')})"
        )

        write_ingest_status(
            "processing",
            filename,
            (
                f"retry "
                f"{next_attempt}/"
                f"{max_attempts}"
            ),
        )

        if process_book(
            filepath,
            settings,
            config,
            retry_entry=retry_entry,
        ):
            successful.append(
                filename
            )

    return successful


# ============================================================
# WATCH LOOP
# ============================================================

def watch_directory():
    global CALIBRE_LIBRARY

    config = load_dirs_config()

    ingest_folder = Path(
        config.get(
            "ingest_folder"
        )
    )

    processed_folder = Path(
        config.get(
            "processed_folder"
        )
    )

    failed_folder = Path(
        config.get(
            "failed_folder"
        )
    )

    tmp_conversion_dir = (
        config.get(
            "tmp_conversion_dir"
        )
    )

    CALIBRE_LIBRARY = str(
        config.get(
            "calibre_library_dir",
            "",
        )
    )

    # --------------------------------------------------------
    # STARTUP
    # --------------------------------------------------------

    configure_calibre_executables()

    for directory in (
        ingest_folder,
        processed_folder,
        failed_folder,
        STATUS_FILE.parent,
        METADATA_CHANGE_LOGS_DIR,
    ):
        ensure_directory(
            directory
        )

    if tmp_conversion_dir:
        ensure_directory(
            tmp_conversion_dir
        )

    retry_file = get_retry_queue_file(
        config
    )

    ensure_directory(
        retry_file.parent
    )

    acquire_instance_lock(
        config
    )

    print("=" * 72)
    print(
        "[WATCHER] Calibre-Web NextGen "
        "CWA-Compatible Ingest Watcher"
    )
    print("=" * 72)

    print(
        f"Ingest Directory:       "
        f"{ingest_folder}"
    )

    print(
        f"Processed Directory:    "
        f"{processed_folder}"
    )

    print(
        f"Failed Directory:       "
        f"{failed_folder}"
    )

    print(
        f"Temp Conversion Dir:    "
        f"{tmp_conversion_dir}"
    )

    print(
        f"Status File:            "
        f"{STATUS_FILE}"
    )

    print(
        f"Retry Queue:            "
        f"{retry_file}"
    )

    print(
        f"Metadata Change Logs:   "
        f"{METADATA_CHANGE_LOGS_DIR}"
    )

    print(
        f"Calibre Library:        "
        f"{CALIBRE_LIBRARY}"
    )

    print(
        f"CWA Settings DB:        "
        f"{CWA_DB}"
    )

    print(
        f"Poll Interval:          "
        f"{POLL_INTERVAL_SECONDS} "
        "seconds"
    )

    print("=" * 72)
    print("")

    write_ingest_status(
        "idle",
        "",
        "watcher active",
    )

    scan_iteration = 0
    last_cleanup = datetime.now()

    while True:
        scan_iteration += 1

        try:
            # Reload every scan so CWA settings changes are live.
            settings = load_cwa_settings()

            timeout_sec = (
                get_processing_timeout(
                    settings
                )
            )

            target_format = (
                get_target_format(
                    settings
                )
            )

            automerge_mode = (
                get_automerge_mode(
                    settings
                )
            )

            stale_temp_minutes = safe_int(
                settings.get(
                    "ingest_stale_temp_minutes",
                    120,
                ),
                120,
                minimum=1,
            )

            stale_interval = safe_int(
                settings.get(
                    "ingest_stale_temp_interval",
                    600,
                ),
                600,
                minimum=1,
            )

            # ------------------------------------------------
            # SETTINGS LOG
            # ------------------------------------------------

            if (
                scan_iteration
                % SETTINGS_LOG_EVERY
                == 0
            ):
                print(
                    "[SETTINGS] "
                    f"auto_convert="
                    f"{settings.get('auto_convert', '?')}, "
                    f"target_format="
                    f"{target_format}, "
                    f"automerge="
                    f"{automerge_mode or 'disabled'}, "
                    f"timeout="
                    f"{timeout_sec // 60}min"
                )

            # ------------------------------------------------
            # TEMP CLEANUP
            # ------------------------------------------------

            if (
                datetime.now()
                - last_cleanup
            ).total_seconds() >= (
                stale_interval
            ):
                cleaned = (
                    cleanup_stale_temps(
                        tmp_conversion_dir,
                        stale_temp_minutes,
                        settings,
                    )
                )

                if cleaned:
                    print(
                        "[CLEANUP] Removed "
                        f"{cleaned} stale "
                        "temp item(s)"
                    )

                last_cleanup = (
                    datetime.now()
                )

            # ------------------------------------------------
            # RETRIES
            # ------------------------------------------------

            if (
                scan_iteration
                % RETRY_SCAN_EVERY
                == 0
            ):
                queue = load_retry_queue(
                    config
                )

                if queue:
                    retried = (
                        process_from_retry_queue(
                            settings,
                            config,
                        )
                    )

                    if retried:
                        print(
                            "[RETRY] Successfully "
                            f"re-processed: "
                            f"{retried}"
                        )

            # ------------------------------------------------
            # NORMAL SCANNER
            # ------------------------------------------------

            retry_queue = load_retry_queue(
                config
            )

            retry_filenames = {
                entry.get(
                    "filename"
                )
                for entry
                in retry_queue
                if (
                    isinstance(
                        entry,
                        dict,
                    )
                    and entry.get(
                        "filename"
                    )
                )
            }

            try:
                files = sorted(
                    (
                        file
                        for file
                        in ingest_folder.iterdir()
                        if (
                            file.is_file()
                            and not file.name.startswith(
                                "."
                            )
                        )
                    ),
                    key=lambda p: (
                        p.name.lower()
                    ),
                )

            except OSError as e:
                print(
                    "[SCAN] Error reading "
                    f"ingest directory "
                    f"{ingest_folder}: {e}"
                )

                files = []

            processed_count = 0

            for filepath in files:
                if (
                    filepath.name
                    in retry_filenames
                ):
                    print(
                        "[SCAN] Skipping "
                        "queued retry: "
                        f"{filepath.name}"
                    )

                    continue

                print(
                    "[SCAN] Processing "
                    f"{filepath.name}..."
                )

                if process_book(
                    filepath,
                    settings,
                    config,
                ):
                    processed_count += 1

            # ------------------------------------------------
            # SCAN SUMMARY
            # ------------------------------------------------

            status = (
                f"Scan #{scan_iteration}: "
                f"{processed_count} "
                "new file(s)"
            )

            queue = load_retry_queue(
                config
            )

            if queue:
                status += (
                    f" | {len(queue)} "
                    "in retry queue"
                )

            print(
                f"[WATCHER] {status}"
            )

            write_ingest_status(
                "idle",
                "",
                status,
            )

        except SystemExit:
            raise

        except Exception as e:
            # A single scan failure must not terminate the daemon.
            print(
                "[WATCHER] Unexpected "
                f"error in scan loop: {e}"
            )

            write_ingest_status(
                "failed",
                "",
                (
                    "watcher error: "
                    f"{str(e)[:100]}"
                ),
            )

        time.sleep(
            POLL_INTERVAL_SECONDS
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        watch_directory()

    except KeyboardInterrupt:
        write_ingest_status(
            "stopped",
            "",
            "keyboard interrupt",
        )

    except RuntimeError as e:
        print(
            f"[FATAL] {e}",
            file=sys.stderr,
        )

        write_ingest_status(
            "stopped",
            "",
            str(e)[:200],
        )

        sys.exit(1)

    except Exception as e:
        print(
            "[FATAL] Unhandled startup "
            f"error: {e}",
            file=sys.stderr,
        )

        write_ingest_status(
            "stopped",
            "",
            (
                "startup error: "
                f"{str(e)[:150]}"
            ),
        )

        sys.exit(1)
