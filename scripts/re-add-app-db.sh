#!bin/bash
if [ -f "/opt/calibre-web-nextgen/config/app.db" ]; then
    echo -e "app.db already exists, updating config_calibre_dir..."
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
systemctl daemon-reload
systemctl start calibre-web-nextgen
systemctl start calibre-web-ingest
systemctl start calibre-web-meta
