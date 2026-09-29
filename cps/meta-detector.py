#!/usr/bin/env python3
#Not currently used, saved here for potential future integration
# /opt/calibre-web-nextgen/scripts/meta_detector.py
# Metadata Change Detector using dirs.json and cwa.db settings

import sqlite3
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime
import os

# Configuration files
DIRS_JSON = Path("/opt/calibre-web-nextgen/dirs.json")
CWA_DB = Path("/opt/calibre-web-nextgen/cwa.db")
META_STATUS_FILE = Path("/opt/calibre-web-nextgen/config/cwa_meta_status")
WEB_UI_HOST = os.environ.get('CWA_PORT_OVERRIDE', 'localhost')
WEB_UI_PORT = 8083  # Default port

def load_dirs_config():
    """Load paths from CWA's dirs.json"""
    default_config = {
        'calibre_library_dir': '/home/ava/Dusty Bookshelf',
        'checkpoint_file': '/opt/calibre-web-nextgen/.meta_checkpoint'
    }
    
    if not DIRS_JSON.exists():
        return default_config
    
    try:
        with open(DIRS_JSON, 'r') as f:
            config = json.load(f)
        
        # Clean up paths
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
        'auto_metadata_detection': 1,  # Assume on by default if not present
        'metadata_check_interval_seconds': 1800,  # Check every minute
        'metadata_sync_on_change': 1  # Trigger web UI sync on change
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
            settings = dict(zip(columns, row))
            conn.close()
            
            # Merge with defaults for missing keys
            return {**default_settings, **settings}
        else:
            conn.close()
            return default_settings
            
    except sqlite3.OperationalError as e:
        print(f"[SETTINGS] Error reading cwa.db: {e}")
        return default_settings

def write_meta_status(state, detail=""):
    """Write metadata detector status"""
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

# Usage in main loop:
write_meta_status("active", "monitoring metadata.db")

def get_metadata_db_path(calibre_library_dir):
    """Get path to metadata.db in Calibre library"""
    # Check standard locations
    possible_paths = [
        Path(calibre_library_dir) / "metadata.db",
        Path(calibre_library_dir),  # metadata.db might be in root
    ]
    
    for path in possible_paths:
        if path.exists() or Path(str(path) + "/metadata.db").exists():
            full_path = path if path.is_file() else path.parent / "metadata.db"
            if full_path.exists():
                return full_path
    
    # If metadata.db is in a book subdirectory, find the library-level one
    meta_path = Path(calibre_library_dir) / "metadata.db"
    if meta_path.exists():
        return meta_path
    
    return None

def get_db_hash(db_path):
    """Get MD5 hash of metadata.db file"""
    if not db_path or not db_path.exists():
        return None
    
    try:
        # Read file size first (quick check)
        file_size = db_path.stat().st_size
        
        # For large files, hash only header + footer (faster)
        if file_size > 1024 * 1024:  # > 1MB
            with open(db_path, 'rb') as f:
                data = f.read(4096)  # First 4KB
                f.seek(-4096, 2)  # Last 4KB
                data += f.read(4096)
        else:
            data = db_path.read_bytes()
        
        return hashlib.md5(data).hexdigest()
    except (OSError, IOError) as e:
        print(f"[META] Error hashing {db_path}: {e}")
        return None

def get_last_checkpoint(checkpoint_path):
    """Load last known hash and timestamp from checkpoint"""
    if not checkpoint_path.exists():
        return None, None
    
    try:
        with open(checkpoint_path, 'r') as f:
            data = json.load(f)
        return data.get('hash'), data.get('timestamp')
    except (json.JSONDecodeError, IOError):
        return None, None

def save_checkpoint(checkpoint_path, hash_value, event_count):
    """Save current hash and stats to checkpoint"""
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
    
    # Method 1: Call the reconnect_database endpoint
    import urllib.request
    import urllib.error
    
    try:
        url = f"http://{WEB_UI_HOST}:{WEB_UI_PORT}/admin/reconnect_database"
        req = urllib.request.Request(url, method='POST')
        response = urllib.request.urlopen(req, timeout=10)
        print(f"[META] ✓ Triggered web UI database reconnect")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        # Method 2: Just log for manual intervention
        print(f"[META] ! Could not trigger web UI sync: {e}")
        print("[META] Consider accessing admin panel to manually reconnect database")
        return False

def load_dirs_config():
    """Load paths from CWA's dirs.json"""
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

def main():
    """Main monitoring loop"""
    # Load configuration
    config = load_dirs_config()
    settings = load_cwa_settings()
    
    calibre_library_dir = config.get('calibre_library_dir')
    checkpoint_path = Path(config.get('checkpoint_file'))
    
    # Find metadata.db
    metadata_db = get_metadata_db_path(calibre_library_dir)
    
    if not metadata_db:
        print(f"[META] ERROR: metadata.db not found in {calibre_library_dir}")
        print("[META] Please ensure Calibre library is properly configured")
        return
    
    check_interval = int(settings.get('metadata_check_interval_seconds', 60))
    
    print("=" * 60)
    print("[META] Calibre-Web NextGen Metadata Change Detector")
    print("=" * 60)
    print(f"Library Path:         {calibre_library_dir}")
    print(f"metadata.db:          {metadata_db}")
    print(f"Checkpoint File:      {checkpoint_path}")
    print(f"Check Interval:       {check_interval}s")
    print(f"Web UI Sync:          {'ON' if int(settings.get('metadata_sync_on_change', 1)) else 'OFF'}")
    print(f"CWA Settings DB:      {CWA_DB}")
    print("=" * 60)
    print("")
    
    # Initialize checkpoint
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    current_hash = get_db_hash(metadata_db)
    
    if current_hash:
        save_checkpoint(checkpoint_path, current_hash, 0)
        print(f"[META] Initialized checkpoint with hash: {current_hash[:8]}...")
    
    event_count = 0
    
    while True:
        # Reload settings periodically (reflects admin panel changes)
        settings = load_cwa_settings()
        check_interval = int(settings.get('metadata_check_interval_seconds', 60))
        
        # Check if metadata detection is enabled
        if not int(settings.get('auto_metadata_detection', 1)):
            print("[META] Metadata detection disabled in settings. Sleeping...")
            time.sleep(check_interval)
            continue
        
        # Get current hash
        new_hash = get_db_hash(metadata_db)
        last_hash, last_timestamp = get_last_checkpoint(checkpoint_path)
        
        # Check for changes
        if new_hash and new_hash != last_hash:
            event_count += 1
            event_age = ""
            if last_timestamp:
                try:
                    dt = datetime.fromisoformat(last_timestamp)
                    age = (datetime.now() - dt).total_seconds()
                    event_age = f" ({age:.0f}s since last event)"
                except:
                    pass
            
            print(f"[META] ✨ CHANGE DETECTED!{event_age}")
            print(f"[META] Old hash: {last_hash[:8] if last_hash else 'N/A'}...")
            print(f"[META] New hash: {new_hash[:8]}...")
            
            # Trigger web UI sync
            sync_with_web_ui(settings)
            
            # Save checkpoint
            save_checkpoint(checkpoint_path, new_hash, event_count)
            
            # Log to stdout for journalctl visibility
            print(f"[META] Event #{event_count}: metadata.db updated")
        
        # Periodic status log (every 10 checks)
        if event_count % 10 == 0:
            print(f"[META] Status: {event_count} change(s) detected | Checking every {check_interval}s")
        
        time.sleep(check_interval)

if __name__ == "__main__":
    main()
