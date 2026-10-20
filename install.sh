#!/bin/sh
# Registers the native messaging host so the SimWeb extension can launch Chrome.
set -e
HOST="$(cd "$(dirname "$0")" && pwd)/host/simweb_host.py"
DIR="$HOME/.config/google-chrome/NativeMessagingHosts"
chmod +x "$HOST"
mkdir -p "$DIR"
cat > "$DIR/com.simweb.host.json" <<EOF
{
  "name": "com.simweb.host",
  "description": "SimWeb launcher",
  "path": "$HOST",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://oijnnjmjkeeneodffamgbmfkpndknhfl/"]
}
EOF
echo "Installed $DIR/com.simweb.host.json"
echo "Now: chrome://extensions -> Developer mode -> Load unpacked -> $(dirname "$HOST")/../extension"
