#!/bin/bash
###############################################################################
# Calibre-Web NextGen Installation Script (Verbose Mode)
# Shows ALL commands and output in real-time
###############################################################################

# Show each command before executing (trace mode)
set -x
# Exit on error
set -e

#######################################
# Configuration (Edit these values)
#######################################
INSTALL_DIR="/opt/calibre-web-nextgen"
CONFIG_DIR="${INSTALL_DIR}/config"
SERVICE_USER="acw"
SERVICE_GROUP="acw"
USER_USER="yourusername"
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
# Check if running as root
#######################################
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}[ERROR]${NC} Please run as root (sudo ./install.sh)"
    exit 1
fi

echo -e "${GREEN}[INSTALL]${NC} Starting Calibre-Web NextGen installation..."
echo ""

#######################################
# Step 1: Install System Dependencies
#######################################
echo -e "${YELLOW}[STEP 1]${NC} Installing system dependencies..."
echo "--- apt-get update ---"
apt-get update
echo "--- apt-get install dependencies ---"
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
    libssl-dev

echo -e "${GREEN}[INSTALL]${NC} System dependencies installed."
echo ""

#######################################
# Step 2: Create Service User
#######################################
echo -e "${YELLOW}[STEP 4]${NC} Creating service user '$SERVICE_USER'..."
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/false -d "$INSTALL_DIR" "$SERVICE_USER"
    echo -e "${GREEN}[INSTALL]${NC} Service user created."
else
    echo -e "${RED}[WARN]${NC} User '$SERVICE_USER' already exists, skipping."
fi
echo ""

usermod -a -G "$SERVICE_GROUP" "$USER_USER"

#######################################
# Step 3: Set Up Virtual Environment
#######################################
echo -e "${YELLOW}[STEP 5]${NC} Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
echo "--- Upgrading pip/setuptools/wheel ---"
python -m pip install --upgrade pip setuptools wheel
echo "--- Installing package dependencies ---"
./venv/bin/python3 -m pip install -e .
echo -e "${GREEN}[INSTALL]${NC} Virtual environment and dependencies installed."
echo ""

#######################################
# Step 4: Create Config Directory
#######################################
echo -e "${YELLOW}[STEP 6]${NC} Creating config directory..."
mkdir -p "$CONFIG_DIR"
ls -la "$CONFIG_DIR"
echo -e "${GREEN}[INSTALL]${NC} Config directory created at $CONFIG_DIR"
echo ""

#######################################
# Step 5: Create Systemd Service Files
#######################################
echo -e "${YELLOW}[STEP 7]${NC} Creating systemd service files..."
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
Environment="TZ=${TIMEZONE}"
ExecStart=${INSTALL_DIR}/venv/bin/python cps.py -p ${CONFIG_DIR}/app.db
Restart=always
RestartSec=10
UMask=022

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/calibre-web-ingest.service << EOF
[Unit]
Description=Calibre-Web NextGen Ingest Service
After=calibre-web-nextgen.service
Requires=calibre-web-nextgen.service

[Service]
Type=simple
User=acw
Group=acw
WorkingDirectory=/opt/calibre-web-nextgen
Environment="PATH=/opt/calibre-web-nextgen/venv/bin"
ExecStart=/opt/calibre-web-nextgen/venv/bin/python /opt/calibre-web-nextgen/cps/auto-ingest.py
Restart=always
RestartSec=15
StandardOutput=journal

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/calibre-web-meta.service << EOF
[Unit]
Description=Calibre-Web NextGen Metadata Change Detector
After=calibre-web-nextgen.service
Requires=calibre-web-nextgen.service

[Service]
Type=simple
User=acw
Group=acw
WorkingDirectory=/opt/calibre-web-nextgen
Environment="PATH=/opt/calibre-web-nextgen/venv/bin"
Environment="CWA_METADATA_CHANGE_LOGS_DIR=/opt/calibre-web-nextgen/config/metadata_change_logs"
Environment="CWA_METADATA_TEMP_DIR=/opt/calibre-web-nextgen/config/metadata_temp"
ExecStart=/bin/bash /opt/calibre-web-nextgen/scripts/metadata-detector.sh
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

echo "--- Service file contents ---"
cat /etc/systemd/system/calibre-web-nextgen.service
cat /etc/systemd/system/calibre-web-ingest.service
cat /etc/systemd/system/calibre-web-meta.service
echo -e "${GREEN}[INSTALL]${NC} Systemd service files created."
echo ""

#######################################
# Step 6: Set Permissions
#######################################
echo -e "${YELLOW}[STEP 8]${NC} Setting file permissions..."
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$INSTALL_DIR"
chmod -R 755 "$INSTALL_DIR"
chmod 775 "$CONFIG_DIR"
echo "--- Permissions set ---"
ls -la "$INSTALL_DIR"
echo ""
echo -e "${RED}[WARN]${NC} IMPORTANT: Run this command for your library:"
echo "   sudo setfacl -R -m u:${SERVICE_USER}:rwx '${CALIBRE_LIBRARY}'"
echo ""

#######################################
# Step 7: Initialize Database
#######################################
echo -e "${YELLOW}[STEP 9]${NC} Initializing application database..."

if [ -f "${CONFIG_DIR}/app.db" ]; then
    echo -e "${RED}[WARN]${NC} app.db already exists, updating config_calibre_dir..."
else
    echo "Starting service once to create app.db schema..."
    systemctl daemon-reload
    systemctl start calibre-web-nextgen
    sleep 10
    echo "Stopping service to configure database..."
    systemctl stop calibre-web-nextgen 2>/dev/null || true
fi

echo "--- Setting library path in database (with proper quoting) ---"
# Use printf to safely handle paths with spaces/special chars
sqlite3 "${CONFIG_DIR}/app.db" << EOF
UPDATE settings SET config_calibre_dir = '${CALIBRE_LIBRARY}' WHERE id = 1;
.quit
EOF

echo "--- Verifying configuration ---"
sqlite3 "${CONFIG_DIR}/app.db" "SELECT config_calibre_dir FROM settings WHERE id = 1;"
echo -e "${GREEN}[INSTALL]${NC} Database initialized with library path: ${CALIBRE_LIBRARY}"
echo ""
#######################################
# Step 8: Enable and Start Service
#######################################
echo -e "${YELLOW}[STEP 10]${NC} Enabling and starting service..."
systemctl daemon-reload
systemctl enable calibre-web-nextgen
systemctl start calibre-web-nextgen

#######################################
# Step 9: Verify Installation
#######################################
echo ""
echo -e "${GREEN}[INSTALL]${NC} ==="
echo -e "${GREEN}[INSTALL]${NC} Installation Complete!"
echo -e "${GREEN}[INSTALL]${NC} ==="
echo ""
echo -e "${YELLOW}[INFO]${NC} Service Status:"
systemctl status calibre-web-nextgen --no-pager
echo ""
echo -e "${YELLOW}[INFO]${NC} Recent Logs:"
journalctl -u calibre-web-nextgen -n 30 --no-pager
echo ""
echo -e "${GREEN}[INSTALL]${NC} ==="
echo -e "${GREEN}[INSTALL]${NC} Access Information"
echo -e "${GREEN}[INSTALL]${NC} ==="
echo -e "${GREEN}[INSTALL]${NC} URL: http://localhost:${PORT}"
echo -e "${GREEN}[INSTALL]${NC} Username: admin"
echo -e "${GREEN}[INSTALL]${NC} Password: admin123"
echo ""
echo -e "${GREEN}[INSTALL]${NC} ==="
echo -e "${GREEN}[INSTALL]${NC} Post-Installation Checklist"
echo -e "${GREEN}[INSTALL]${NC} ==="
echo ""
echo "Run these commands:"
echo "  1. sudo setfacl -R -m u:${SERVICE_USER}:rwx '${CALIBRE_LIBRARY}'"
echo "  2. sudo usermod -a -G ${SERVICE_USER} yourUsername"
echo "  3. Change admin password in Admin > Edit User"
echo ""

# Final check
if systemctl is-active --quiet calibre-web-nextgen; then
    echo -e "${GREEN}[SUCCESS]${NC} ✓ Service is running successfully!"
else
    echo -e "${RED}[FAILED]${NC} ✗ Service failed to start. Check logs above."
fi

# Turn off trace mode at the end
set +x
echo ""
echo -e "${GREEN}[INSTALL]${NC} Script completed."
