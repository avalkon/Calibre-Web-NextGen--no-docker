#!/usr/bin/env python3
# /opt/calibre-web-nextgen/scripts/meta_detector.py
# Metadata Change Detector with Full Status File Support

import sqlite3
import hashlib
import json
from pathlib import Path
import time
import signal
import sys
from datetime import datetime
import os

# Configuration files
DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")

# Status tracking files (matches CWA format)
META_STATUS_FILE = Path("/opt/calibre-web-nextgen/config/cwa_meta_status")

def load_dirs_config():
    """Load paths from dirs.json"""
    default_config = {
        'calibre_library_dir': '/home/ava/Dusty Bookshelf',
        'checkpoint_file': '/opt/calibre-web-nextgen/.meta_checkpoint'
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
    """Read settings from cwa.db"""
    default_settings = {
        'metadata_check_interval_seconds': 60,
        'metadata_sync_on_change': 1
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
            return {**default_settings, **dict(zip(columns, row))}
        else:
            conn.close()
            return default_settings
            
    except sqlite3.OperationalError:
        return default_settings

def write_meta_status(state, detail=""):
    """Write metadata detector status: state:timestamp:detail"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    status_line = f"{state}:{timestamp}:{detail}"
    
    try:
        META_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(META_STATUS_FILE, 'w') as f:
            f.write(status_line)
    except IOError as e:
        print(f"[META STATUS] Error writing status file: {e}")

def clear_meta_status():
    """Clear status file when idle"""
    try:
        if META_STATUS_FILE.exists():
            META_STATUS_FILE.unlink()
    except IOError:
        pass

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print("\n[SHUTDOWN] Received termination signal...")
    write_meta_status("stopped", "shutdown requested")
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

def get_metadata_db_path(calibre_library_dir):
    """Get path to metadata.db in Calibre library"""
    meta_path = Path(calibre_library_dir) / "metadata.db"
    return meta_path if meta_path.exists() else None

def get_db_hash(db_path):
    """Get MD5 hash of metadata.db file"""
    if not db_path or not db_path.exists():
        return None
    
    try:
        file_size = db_path.stat().st_size
        
        if file_size > 1024 * 1024:  # > 1MB
            with open(db_path, 'rb') as f:
                data = f.read(4096)
                f.seek(-4096, 2)
                data += f.read(4096)
        else:
            data = db_path.read_bytes()
        
        return hashlib.md5(data).hexdigest()
    except (OSError, IOError):
        return None

def get_last_checkpoint(checkpoint_path):
    """Load last known hash"""
    if not checkpoint_path.exists():
        return None, None
    
    try:
        with open(checkpoint_path, 'r') as f:
            data = json.load(f)
        return data.get('hash'), data.get('timestamp')
    except (json.JSONDecodeError, IOError):
        return None, None

def save_checkpoint(checkpoint_path, hash_value, event_count):
    """Save current hash and stats"""
    checkpoint_data = {
        'hash': hash_value,
        'timestamp': datetime.now().isoformat(),
        'event_count': event_count
    }
    
    try:
        with open(checkpoint_path, 'w') as f:
            json.dump(checkpoint_data, f, indent=2)
    except IOError as e:
        print(f"[META] Error saving checkpoint: {e}")

def sync_with_web_ui(settings):
    """Trigger web app to reload library metadata"""
    if not int(settings.get('metadata_sync_on_change', 1)):
        print("[META] Web UI sync disabled in settings")
        return False
    
    import urllib.request
    import urllib.error
    
    try:
        port = os.environ.get('CWA_PORT_OVERRIDE', '8083')
        url = f"http://localhost:{port}/admin/reconnect_database"
        req = urllib.request.Request(url, method='POST')
        response = urllib.request.urlopen(req, timeout=10)
        print(f"[META] ✓ Triggered web UI database reconnect")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[META] ! Could not trigger web UI sync: {e}")
        return False

def main():
    """Main monitoring loop with status file updates"""
    config = load_dirs_config()
    settings = load_cwa_settings()
    
    calibre_library_dir = config.get('calibre_library_dir')
    checkpoint_path = Path(config.get('checkpoint_file'))
    metadata_db = get_metadata_db_path(calibre_library_dir)
    
    if not metadata_db:
        print(f"[META] ERROR: metadata.db not found in {calibre_library_dir}")
        write_meta_status("error", "metadata.db not found")
        return
    
    check_interval = int(settings.get('metadata_check_interval_seconds', 60))
    
    print("=" * 60)
    print("[META] Calibre-Web NextGen Metadata Change Detector")
    print("=" * 60)
    print(f"Library Path:           {calibre_library_dir}")
    print(f"metadata.db:            {metadata_db}")
    print(f"Status File:            {META_STATUS_FILE}")
    print(f"Checkpoint File:        {checkpoint_path}")
    print(f"Check Interval:         {check_interval}s")
    print("=" * 60)
    print("")
    
    # Initialize checkpoint
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    current_hash = get_db_hash(metadata_db)
    
    if current_hash:
        save_checkpoint(checkpoint_path, current_hash, 0)
        write_meta_status("active", "monitoring metadata.db")
        print(f"[META] Initialized with hash: {current_hash[:8]}...")
    
    event_count = 0
    
    while True:
        # Reload settings periodically
        settings = load_cwa_settings()
        check_interval = int(settings.get('metadata_check_interval_seconds', 60))
        
        # Check if metadata detection is enabled
        if not int(settings.get('auto_metadata_detection', 1)):
            write_meta_status("paused", "disabled in settings")
            time.sleep(check_interval)
            continue
        
        # Get current hash
        new_hash = get_db_hash(metadata_db)
        last_hash, last_timestamp = get_last_checkpoint(checkpoint_path)
        
        # Check for changes
        if new_hash and new_hash != last_hash:
            event_count += 1
            print(f"[META] ✨ CHANGE DETECTED! Event #{event_count}")
            print(f"[META] Old hash: {last_hash[:8] if last_hash else 'N/A'}...")
            print(f"[META] New hash: {new_hash[:8]}...")
            
            write_meta_status("changed", f"event #{event_count}")
            
            # Trigger web UI sync
            sync_with_web_ui(settings)
            
            # Save checkpoint
            save_checkpoint(checkpoint_path, new_hash, event_count)
            
            # Return to monitoring state
            write_meta_status("active", "monitoring metadata.db")
        else:
            # Periodic status update (every 10 checks)
            if event_count % 10 == 0:
                write_meta_status("active", f"checked, no changes (#{event_count})")
        
        time.sleep(check_interval)

if __name__ == "__main__":
    main()
