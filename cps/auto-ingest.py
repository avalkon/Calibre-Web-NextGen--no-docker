#!/usr/bin/env python3
# /srv/calibre-ingest/watched_ingest.py
# CWA-Compatible Ingest Watcher with Full Status File Support

import os
import subprocess
import sqlite3
import json
import signal
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta

# Configuration files
DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")

# Status tracking files (matches CWA format exactly)
STATUS_FILE = Path("/opt/calibre-web-nextgen/config/cwa_ingest_status")
RETRY_QUEUE_FILE = Path("/opt/calibre-web-nextgen/config/cwa_ingest_retry_queue")

def load_dirs_config():
    """Load paths from CWA's dirs.json"""
    default_config = {
        'ingest_folder': '/srv/calibre-ingest',
        'calibre_library_dir': '/home/ava/Dusty Bookshelf',
        'tmp_conversion_dir': '/tmp/cwa_conversions',
        'processed_folder': '/srv/calibre-ingest/processed',
        'failed_folder': '/srv/calibre-ingest/failed',
        'retry_queue_file': str(RETRY_QUEUE_FILE),
        'max_retry_attempts': 3,
        'retry_interval_seconds': 300
    }
    
    if not DIRS_JSON.exists():
        return default_config
    
    try:
        with open(DIRS_JSON, 'r') as f:
            config = json.load(f)
        
        for key in config:
            val = config[key].strip() if isinstance(config[key], str) else config[key]
            if isinstance(val, str) and val.startswith("'") and val.endswith("'"):
                config[key] = val[1:-1]
        
        return {**default_config, **config}
    except Exception as e:
        print(f"[CONFIG] Error reading dirs.json: {e}")
        return default_config

def load_cwa_settings():
    """Read settings from cwa.db cwa_settings table"""
    default_settings = {
        'auto_convert': 1,
        'auto_convert_target_format': 'epub',
        'auto_ingest_automerge': 0,
        'auto_ingest_ignored_formats': '',
        'auto_convert_ignored_formats': '',
        'auto_convert_retained_formats': '',
        'ingest_timeout_minutes': 15,
        'ingest_stale_temp_minutes': 120,
        'ingest_stale_temp_interval': 600
    }
    
    if not CWA_DB.exists():
        return default_settings
    
    try:
        conn = sqlite3.connect(CWA_DB)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM cwa_settings LIMIT 1")
        row = cursor.fetchone()
        
        if row:
            columns = [desc[0] for desc in cursor.description]
            conn.close()
            return dict(zip(columns, row))
        else:
            conn.close()
            return default_settings
            
    except sqlite3.OperationalError as e:
        print(f"[SETTINGS] Error reading cwa.db: {e}")
        return default_settings

# ============================================================
# STATUS FILE FUNCTIONS (CWA Compatible Format)
# ============================================================

def write_ingest_status(state, filename="", detail=""):
    """
    Write status file in CWA format: state:filename:timestamp:detail
    
    Possible states: idle, processing, failed, stopped
    Format: state:filename:YYYY-MM-DD HH:MM:SS:detail
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status_line = f"{state}:{filename}:{timestamp}:{detail}"
    
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATUS_FILE, 'w') as f:
            f.write(status_line)
        print(f"[STATUS] {state.upper()}: {filename or 'idle'}")
    except IOError as e:
        print(f"[STATUS] Error writing status file: {e}")

def clear_ingest_status():
    """Clear status file when idle (optional - can also write idle state)"""
    try:
        if STATUS_FILE.exists():
            STATUS_FILE.unlink()
    except IOError:
        pass

def load_retry_queue():
    """Load retry queue from JSON file"""
    if not RETRY_QUEUE_FILE.exists():
        return []
    
    try:
        with open(RETRY_QUEUE_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []

def save_retry_queue(queue_data):
    """Save retry queue to JSON file"""
    try:
        RETRY_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(RETRY_QUEUE_FILE, 'w') as f:
            json.dump(queue_data, f, indent=2)
    except IOError as e:
        print(f"[RETRY] Error saving queue: {e}")

# ============================================================
# SHUTDOWN HANDLER
# ============================================================

def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    print("\n[SHUTDOWN] Received termination signal...")
    write_ingest_status("stopped", "", "shutdown requested")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ============================================================
# PARSING & VALIDATION FUNCTIONS
# ============================================================

def parse_format_list(formats_str):
    """Parse comma-separated format list"""
    if not formats_str:
        return set()
    
    return {'.' + fmt.strip().lower() if not fmt.strip().startswith('.') else fmt.strip().lower() 
            for fmt in formats_str.split(',') if fmt.strip()}

def get_processing_timeout(settings):
    """Get timeout for processing in seconds"""
    timeout_min = int(settings.get('ingest_timeout_minutes', 15))
    return timeout_min * 60

# ============================================================
# CLEANUP FUNCTIONS
# ============================================================

def cleanup_stale_temps(tmp_dir, stale_minutes, settings):
    """Remove temp files older than threshold"""
    tmp_path = Path(tmp_dir)
    
    if not tmp_path.exists():
        return 0
    
    cutoff_time = datetime.now() - timedelta(minutes=stale_minutes)
    cleaned = 0
    
    for file in tmp_path.rglob('*'):
        if file.is_file():
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
# CONVERSION & LIBRARY FUNCTIONS
# ============================================================

def convert_file(source_path, target_format, tmp_dir=None, timeout_sec=1800):
    """Convert using ebook-convert with timeout"""
    output_path = source_path.with_suffix(f'.{target_format}')
    
    env = os.environ.copy()
    if tmp_dir:
        env['TEMP'] = tmp_dir
        env['TMP'] = tmp_dir
        env['TMPDIR'] = tmp_dir
    
    cmd = ['ebook-convert', str(source_path), str(output_path)]
    
    try:
        write_ingest_status("processing", source_path.name, "conversion started")
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout_sec)
        
        if result.returncode == 0:
            print(f"[CONVERT] ✓ {source_path.name} → {output_path.name}")
            write_ingest_status("processing", source_path.name, "conversion complete")
            return output_path
        else:
            print(f"[CONVERT] ✗ {source_path.name}: {result.stderr[:500]}")
            write_ingest_status("failed", source_path.name, f"conversion error: {result.stderr[:100]}")
            return None
            
    except subprocess.TimeoutExpired:
        print(f"[CONVERT] ⏱️ TIMEOUT after {timeout_sec}s for {source_path.name}")
        write_ingest_status("failed", source_path.name, "conversion timeout")
        return None
    except Exception as e:
        print(f"[CONVERT] ✗ {source_path.name}: {e}")
        write_ingest_status("failed", source_path.name, str(e)[:100])
        return None

def add_to_library(filepath, automerge=False):
    """Add file to Calibre library using calibredb"""
    cmd = ['calibredb', 'add', '--library-path', CALIBRE_LIBRARY, str(filepath)]
    
    if automerge:
        cmd.append('--automergeresult')
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=get_processing_timeout(load_cwa_settings()))
        
        if result.returncode == 0:
            return True
        else:
            print(f"[LIBRARY] ✗ {filepath.name}: {result.stderr[:500]}")
            return False
            
    except subprocess.TimeoutExpired:
        print(f"[LIBRARY] ⏱️ TIMEOUT adding {filepath.name}")
        return False
    except Exception as e:
        print(f"[LIBRARY] ✗ {filepath.name}: {e}")
        return False

def add_to_retry_queue(filepath, error_msg, config):
    """Add file to retry queue"""
    queue_data = load_retry_queue()
    
    # Remove any existing entry for this file
    queue_data = [e for e in queue_data if e.get('filename') != filepath.name]
    
    retry_entry = {
        'filename': filepath.name,
        'original_path': str(filepath),
        'attempts': 1,
        'max_attempts': int(config.get('max_retry_attempts', 3)),
        'last_attempt': datetime.now().isoformat(),
        'error': str(error_msg)[:500]
    }
    
    queue_data.append(retry_entry)
    save_retry_queue(queue_data)
    print(f"[RETRY] Added to queue: {filepath.name} (attempt 1/{retry_entry['max_attempts']})")

# ============================================================
# BOOK PROCESSING PIPELINE
# ============================================================

def should_ingest(filepath, settings):
    """Check if file should be ingested (filter ignored formats)"""
    ignored = parse_format_list(settings.get('auto_ingest_ignored_formats', ''))
    if filepath.suffix.lower() in ignored:
        return False, f"format {filepath.suffix} is in ingest ignore list"
    return True, "allowed"

def should_convert(source_ext, settings):
    """Determine if file should be converted"""
    if not int(settings.get('auto_convert', 1)):
        return False, None, "auto_convert disabled"
    
    target_format = settings.get('auto_convert_target_format', 'epub').lower().replace('.', '')
    target_ext = f'.{target_format}'
    
    retained = parse_format_list(settings.get('auto_convert_retained_formats', ''))
    ignored_conv = parse_format_list(settings.get('auto_convert_ignored_formats', ''))
    
    if source_ext in retained:
        return False, source_ext, "format is retained"
    if source_ext in ignored_conv:
        return False, source_ext, "format is ignored for conversion"
    if source_ext == target_ext:
        return False, source_ext, "already target format"
    
    return True, target_ext, "convert needed"

def process_book(filepath, settings, config):
    """Full processing pipeline with status tracking"""
    # Check if should ingest
    allowed, reason = should_ingest(filepath, settings)
    if not allowed:
        print(f"[INGEST] Skip {filepath.name}: {reason}")
        return False
    
    source_ext = filepath.suffix.lower()
    
    # Check if should convert
    needs_conversion, target_or_source, reason = should_convert(source_ext, settings)
    
    if needs_conversion:
        tmp_dir = config.get('tmp_conversion_dir')
        timeout_sec = get_processing_timeout(settings)
        converted = convert_file(filepath, target_or_source.replace('.', ''), tmp_dir, timeout_sec)
        
        if converted:
            filepath = converted
            source_ext = converted.suffix.lower()
        else:
            # Failed conversion - add to retry queue
            add_to_retry_queue(filepath, "Conversion failed", config)
            write_ingest_status("idle", "", "file queued for retry")
            return False
    
    # Add to library
    automerge = bool(int(settings.get('auto_ingest_automerge', 0)))
    success = add_to_library(filepath, automerge)
    
    if success:
        processed_folder = Path(config.get('processed_folder'))
        dest = processed_folder / filepath.name
        filepath.rename(dest)
        print(f"[INGEST] ✓ Imported: {dest.name}")
        write_ingest_status("idle", "", "processing complete")
        return True
    else:
        # Failed - add to retry queue
        add_to_retry_queue(filepath, "Library add failed", config)
        write_ingest_status("idle", "", "file queued for retry")
        return False

def process_from_retry_queue(settings, config):
    """Process files from retry queue that are ready"""
    max_interval = int(config.get('retry_interval_seconds', 300))
    queue_data = load_retry_queue()
    processed = []
    
    for entry in queue_data[:]:
        # Check if enough time has passed
        last_attempt = datetime.fromisoformat(entry['last_attempt'])
        if datetime.now() - last_attempt < timedelta(seconds=max_interval):
            continue
        
        # Check if we've exceeded max attempts
        if entry['attempts'] >= entry['max_attempts']:
            print(f"[RETRY] Max attempts ({entry['max_attempts']}) reached for {entry['filename']}")
            queue_data.remove(entry)
            continue
        
        # Attempt retry
        filepath = Path(entry['original_path'])
        if not filepath.exists():
            print(f"[RETRY] File no longer exists: {entry['filename']}")
            queue_data.remove(entry)
            continue
        
        print(f"[RETRY] Attempt {entry['attempts'] + 1}/{entry['max_attempts']} for {entry['filename']}")
        write_ingest_status("processing", entry['filename'], "retry started")
        
        if process_book(filepath, settings, config):
            queue_data.remove(entry)
            processed.append(entry['filename'])
        else:
            entry['attempts'] += 1
            entry['last_attempt'] = datetime.now().isoformat()
    
    save_retry_queue(queue_data)
    return processed

# ============================================================
# MAIN WATCH LOOP
# ============================================================

def watch_directory():
    """Main polling loop with status file updates"""
    global CALIBRE_LIBRARY
    
    # Load paths from dirs.json at startup
    config = load_dirs_config()
    INGEST_FOLDER = Path(config.get('ingest_folder'))
    CALIBRE_LIBRARY = config.get('calibre_library_dir')
    
    # Create all necessary directories
    for directory in [INGEST_FOLDER, config.get('processed_folder'), config.get('failed_folder'), STATUS_FILE.parent]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("[WATCHER] Calibre-Web NextGen CWA-Compatible Ingest Watcher")
    print("=" * 60)
    print(f"Ingest Directory:       {INGEST_FOLDER}")
    print(f"Status File:            {STATUS_FILE}")
    print(f"Retry Queue:            {RETRY_QUEUE_FILE}")
    print(f"Calibre Library:        {CALIBRE_LIBRARY}")
    print(f"CWA Settings DB:        {CWA_DB}")
    print(f"Poll Interval:          10 seconds")
    print("=" * 60)
    print("")
    
    # Initialize status file - show we're running/idle
    write_ingest_status("idle", "", "watcher active")
    
    scan_iteration = 0
    last_cleanup = datetime.now()
    
    while True:
        scan_iteration += 1
        
        # Reload settings each scan (reflects admin panel changes in real-time!)
        settings = load_cwa_settings()
        
        # Get timeout settings
        timeout_sec = get_processing_timeout(settings)
        stale_temp_min = int(settings.get('ingest_stale_temp_minutes', 120))
        stale_interval = int(settings.get('ingest_stale_temp_interval', 600))
        
        # Log settings every 10 scans
        if scan_iteration % 10 == 0:
            print(f"[SETTINGS] auto_convert={settings.get('auto_convert', '?')}, "
                  f"target_format={settings.get('auto_convert_target_format', '?')}, "
                  f"timeout={timeout_sec // 60}min")
            
            # Stale temp cleanup
            if (datetime.now() - last_cleanup).total_seconds() >= stale_interval:
                tmp_dir = config.get('tmp_conversion_dir')
                cleaned = cleanup_stale_temps(tmp_dir, stale_temp_min, settings)
                if cleaned > 0:
                    print(f"[CLEANUP] Removed {cleaned} stale temp file(s)")
                last_cleanup = datetime.now()
        
        # Process new files in ingest folder
        files = [f for f in INGEST_FOLDER.iterdir() 
                 if f.is_file() and not f.name.startswith('.')]
        
        processed_count = 0
        for filepath in files:
            print(f"[SCAN] Processing {filepath.name}...")
            if process_book(filepath, settings, config):
                processed_count += 1
        
        # Process retry queue (every 5 scans to avoid overwhelming)
        if scan_iteration % 5 == 0:
            queue = load_retry_queue()
            if queue:
                retried = process_from_retry_queue(settings, config)
                if retried:
                    print(f"[RETRY] Successfully re-processed: {retried}")
                queue_after = load_retry_queue()
                if len(queue_after) < len(queue):
                    write_ingest_status("idle", "", f"retry queue: {len(queue_after)} remaining")
        
        status = f"Scan #{scan_iteration}: {processed_count} new file(s)"
        queue = load_retry_queue()
        if queue:
            status += f" | {len(queue)} in retry queue"
        
        print(f"[WATCHER] {status}")
        time.sleep(10)

if __name__ == "__main__":
    watch_directory()
