#!/bin/bash
###############################################################################
# Calibre-Web NextGen Installation Script
# Based on manual installation process for github.com/new-usemame/Calibre-NextGen
###############################################################################

set -e  # Exit on error

#######################################
# Configuration (Edit these values)
#######################################
INSTALL_DIR="/opt/calibre-web-nextgen"
CONFIG_DIR="${INSTALL_DIR}/config"
SERVICE_USER="acw"
SERVICE_GROUP="acw"
USER_USER="youusername"
CALIBRE_LIBRARY="/path/to/your/library"
PORT="8083"
TIMEZONE="Etc/UTC"
INSTALL_LOG="/tmp/calibre-web-nextgen-install.log"

#######################################
# Colors for output
#######################################
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

#######################################
# Logging function
#######################################
log() {
    echo -e "${GREEN}[INSTALL]${NC} $1" | tee -a "$INSTALL_LOG"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "$INSTALL_LOG"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "$INSTALL_LOG"
    exit 1
}

#######################################
# Check if running as root
#######################################
if [ "$EUID" -ne 0 ]; then 
    error "Please run as root (sudo ./calwebng_install.sh)"
fi

#######################################
# Step 1: Install System Dependencies
#######################################
log "Installing system dependencies..."
apt-get update >> "$INSTALL_LOG" 2>&1
apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    git \
    sqlite3 \
    curl \
    zip \
    imagemagick \
    python3-dev \
    libldap2-dev \
    libsasl2-dev \
    libssl-dev >> "$INSTALL_LOG" 2>&1

log "System dependencies installed."


#######################################
# Step 2: Create Service User
#######################################
log "Creating service user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false -d "$INSTALL_DIR" "$SERVICE_USER"
    log "Service user created."
else
    warn "User '$SERVICE_USER' already exists, skipping."
fi

usermod -a -G "$SERVICE_GROUP" "$USER_USER"

#######################################
# Step 3: Set Up Virtual Environment
#######################################
log "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip setuptools wheel >> "$INSTALL_LOG" 2>&1
./venv/bin/python3 -m pip install -e . >> "$INSTALL_LOG" 2>&1
log "Virtual environment and dependencies installed."

#######################################
# Step 4: Create Config Directory
#######################################
log "Creating config directory..."
mkdir -p "$CONFIG_DIR"
log "Config directory created at $CONFIG_DIR"

#######################################
# Step 5: Create Systemd Service File
#######################################
log "Creating systemd service file..."
cat > /etc/systemd/system/calibre-web-nextgen.service << EOF
[Unit]
Description=Calibre-Web NextGen
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
Environment="PATH=${INSTALL_DIR}/venv/bin"
ExecStart=${INSTALL_DIR}/venv/bin/python cps.py -p ${CONFIG_DIR}/app.db
Restart=always
RestartSec=10
UMask=022

[Install]
WantedBy=multi-user.target
EOF
log "Systemd service file created."

#######################################
# Step 6: Set Permissions
#######################################
log "Setting file permissions..."
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 775 "$CONFIG_DIR"
setfacl -R -m u:${SERVICE_GROUP}:rwx ${CALIBRE_LIBRARY}

# Note: Adjust this to YOUR actual library path
warn "IMPORTANT: Run 'sudo setfacl -R -m u:${SERVICE_GROUP}:rwx ${CALIBRE_LIBRARY}' for your library access"

#######################################
# Step 7: Initialize Database with Library Path
#######################################
log "Initializing application database with library path..."

if [ -f "${CONFIG_DIR}/app.db" ]; then
    warn "app.db already exists, updating config_calibre_dir..."
else
    log "Starting service once to create app.db schema..."
    systemctl daemon-reload
    systemctl start calibre-web-nextgen
    sleep 10
    systemctl stop calibre-web-nextgen 2>/dev/null || true
fi

# Set the library directory in settings table
sqlite3 "${CONFIG_DIR}/app.db" <<EOF
UPDATE settings SET config_calibre_dir = '${CALIBRE_LIBRARY}' WHERE id = 1;
.quit
EOF

log "Database initialized with library path: ${CALIBRE_LIBRARY}"

#######################################
# Step 8: Enable and Start Service
#######################################
log "Enabling and starting service..."
systemctl daemon-reload
systemctl enable calibre-web-nextgen
systemctl start calibre-web-nextgen

#######################################
# Step 9: Verify Installation
#######################################
log ""
log "=========================================="
log "Installation Complete!"
log "=========================================="
log ""
log "Service Status:"
systemctl status calibre-web-nextgen --no-pager | head -15
log ""
log "Recent Logs:"
journalctl -u calibre-web-nextgen -n 20 --no-pager
log ""
log "=========================================="
log "Access Information"
log "=========================================="
log "URL: http://localhost:${PORT}"
log "Username: admin"
log "Password: admin123"
log ""
log "IMPORTANT POST-INSTALLATION STEPS:"
log "1. Set ACL on your library directory:"
log "   sudo setfacl -R -m u:${SERVICE_USER}:rwx ${CALIBRE_LIBRARY}"
log "2. Change admin password immediately in Admin > Edit User"
log "3. Configure uploads in Admin > Basic Configuration"
log ""
log "Installation log saved to: $INSTALL_LOG"
log "=========================================="

# Final check
if systemctl is-active --quiet calibre-web-nextgen; then
    log "✓ Service is running successfully!"
else
    warn "✗ Service failed to start. Check logs above."
fi
