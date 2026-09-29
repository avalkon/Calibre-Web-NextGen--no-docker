#!/usr/bin/env python3
# /srv/calibre-ingest/watched_ingest.py
# CWA-Compatible Ingest Watcher with timeout and stale temp cleanup

import os
import subprocess
import sqlite3
import json
import signal
from pathlib import Path
import time
from datetime import datetime, timedelta

# Configuration files
DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")

def load_dirs_config():
    """Load paths from CWA's dirs.json"""
    default_config = {
        'ingest_folder': '/srv/calibre-ingest',
        'calibre_library_dir': '/calibre-library',
        'tmp_conversion_dir': '/tmp/cwa_conversions',
        'processed_folder': '/srv/calibre-ingest/processed',
        'failed_folder': '/srv/calibre-ingest/failed',
        'retry_queue_file': '/srv/calibre-ingest/retry_queue.json',
        'max_retry_attempts': 3,
        'retry_interval_seconds': 600
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
            return dict(zip(columns, row))
        else:
            conn.close()
            return default_settings
            
    except sqlite3.OperationalError as e:
        print(f"[SETTINGS] Error reading cwa.db: {e}")
        return default_settings

def parse_format_list(formats_str):
    """Parse comma-separated format list"""
    if not formats_str:
        return set()
    
    return {'.' + fmt.strip().lower() if not fmt.strip().startswith('.') else fmt.strip().lower() 
            for fmt in formats_str.split(',') if fmt.strip()}

def get_processing_timeout(settings):
    """Get timeout for processing in seconds"""
    timeout_min = int(settings.get('ingest_timeout_minutes', 15))
    return timeout_min * 60  # Convert to seconds

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
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            env=env,
            timeout=timeout_sec  # Apply timeout
        )
        
        if result.returncode == 0:
            print(f"[CONVERT] ✓ {source_path.name} → {output_path.name} ({result.stderr.count('\\n')} lines)")
            return output_path
        else:
            print(f"[CONVERT] ✗ {source_path.name}: {result.stderr[:500]}")
            return None
            
    except subprocess.TimeoutExpired:
        print(f"[CONVERT] ⏱️ TIMEOUT after {timeout_sec}s for {source_path.name}")
        return None
    except Exception as e:
        print(f"[CONVERT] ✗ {source_path.name}: {e}")
        return None

def add_to_retry_queue(queue_path, max_retries, filepath, error_msg):
    """Add file to retry queue"""
    if queue_path.exists():
        try:
            with open(queue_path, 'r') as f:
                queue_data = json.load(f)
        except:
            queue_data = []
    else:
        queue_data = []
    
    # Remove existing entry for this file
    queue_data = [e for e in queue_data if e.get('filename') != filepath.name]
    
    retry_entry = {
        'filename': filepath.name,
        'original_path': str(filepath),
        'attempts': 1,
        'max_attempts': max_retries,
        'last_attempt': datetime.now().isoformat(),
        'error': str(error_msg)[:500]
    }
    
    queue_data.append(retry_entry)
    
    with open(queue_path, 'w') as f:
        json.dump(queue_data, f, indent=2)

def process_book(filepath, settings, config):
    """Full processing pipeline with timeout"""
    # Get timeout from settings
    timeout_sec = get_processing_timeout(settings)
    
    # Check if should ingest (filter ignored formats)
    ignored = parse_format_list(settings.get('auto_ingest_ignored_formats', ''))
    if filepath.suffix.lower() in ignored:
        print(f"[INGEST] Skip {filepath.name}: in ignore list")
        return False
    
    source_ext = filepath.suffix.lower()
    
    # Check if should convert
    if int(settings.get('auto_convert', 1)):
        target_format = settings.get('auto_convert_target_format', 'epub').lower().replace('.', '')
        target_ext = f'.{target_format}'
        
        retained = parse_format_list(settings.get('auto_convert_retained_formats', ''))
        ignored_conv = parse_format_list(settings.get('auto_convert_ignored_formats', ''))
        
        if source_ext in retained or source_ext in ignored_conv or source_ext == target_ext:
            pass  # No conversion needed
        else:
            tmp_dir = config.get('tmp_conversion_dir')
            converted = convert_file(filepath, target_format, tmp_dir, timeout_sec)
            if converted:
                filepath = converted
            else:
                # Add to retry queue if conversion fails
                add_to_retry_queue(
                    Path(config.get('retry_queue_file')),
                    int(config.get('max_retry_attempts', 3)),
                    filepath,
                    "Conversion failed"
                )
                return False
    
    # Add to library (also with timeout)
    automerge = bool(int(settings.get('auto_ingest_automerge', 0)))
    cmd = ['calibredb', 'add', '--library-path', config.get('calibre_library_dir'), str(filepath)]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        
        if result.returncode == 0:
            processed_folder = Path(config.get('processed_folder'))
            dest = processed_folder / filepath.name
            filepath.rename(dest)
            print(f"[INGEST] ✓ Imported: {dest.name}")
            return True
        else:
            print(f"[LIBRARY] ✗ {filepath.name}: {result.stderr[:500]}")
            add_to_retry_queue(
                Path(config.get('retry_queue_file')),
                int(config.get('max_retry_attempts', 3)),
                filepath,
                "Library add failed"
            )
            return False
            
    except subprocess.TimeoutExpired:
        print(f"[LIBRARY] ⏱️ TIMEOUT adding {filepath.name}")
        add_to_retry_queue(
            Path(config.get('retry_queue_file')),
            int(config.get('max_retry_attempts', 3)),
            filepath,
            "Library add timeout"
        )
        return False

def watch_directory():
    """Main polling loop with stale temp cleanup"""
    global CALIBRE_LIBRARY
    
    config = load_dirs_config()
    INGEST_FOLDER = Path(config.get('ingest_folder'))
    CALIBRE_LIBRARY = config.get('calibre_library_dir')
    
    for directory in [INGEST_FOLDER, config.get('processed_folder'), config.get('failed_folder')]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    
    print("=" * 60)
    print("[WATCHER] Calibre-Web NextGen CWA-Compatible Ingest Watcher")
    print("=" * 60)
    print(f"Ingest:               {INGEST_FOLDER}")
    print(f"Temp Directory:       {config.get('tmp_conversion_dir')}")
    print(f"Processed:            {config.get('processed_folder')}")
    print(f"Failed:               {config.get('failed_folder')}")
    print(f"CWA Settings:         {CWA_DB}")
    print("=" * 60)
    
    scan_iteration = 0
    last_cleanup = datetime.now()
    
    while True:
        scan_iteration += 1
        
        # Reload settings each scan
        settings = load_cwa_settings()
        
        # Get timeout settings
        timeout_sec = get_processing_timeout(settings)
        stale_temp_min = int(settings.get('ingest_stale_temp_minutes', 120))
        stale_interval = int(settings.get('ingest_stale_temp_interval', 600))
        
        # Log settings every 10 scans
        if scan_iteration % 10 == 0:
            print(f"[SETTINGS] auto_convert={settings.get('auto_convert', '?')}, "
                  f"target_format={settings.get('auto_convert_target_format', '?')}, "
                  f"timeout={timeout_sec // 60}min, stale_check={stale_interval // 60}min")
            
            # Stale temp cleanup (run at configured interval)
            if (datetime.now() - last_cleanup).total_seconds() >= stale_interval:
                tmp_dir = config.get('tmp_conversion_dir')
                cleaned = cleanup_stale_temps(tmp_dir, stale_temp_min, settings)
                if cleaned > 0:
                    print(f"[CLEANUP] Removed {cleaned} stale temp file(s)")
                last_cleanup = datetime.now()
        
        # Process new files
        files = [f for f in INGEST_FOLDER.iterdir() 
                 if f.is_file() and not f.name.startswith('.')]
        
        processed_count = 0
        for filepath in files:
            print(f"[SCAN] {filepath.name}...")
            if process_book(filepath, settings, config):
                processed_count += 1
        
        print(f"[WATCHER] Scan #{scan_iteration}: {processed_count} file(s) processed | Timeout: {timeout_sec // 60}min")
        
        time.sleep(10)

if __name__ == "__main__":
    watch_directory()
