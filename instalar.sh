#!/usr/bin/env bash
# Instala el Panel de servicios para el usuario actual.
#   ./instalar.sh          versión de terminal (comando «servicios») y app de escritorio
#   ./instalar.sh --solo-terminal   solo el comando «servicios» (p. ej. en un servidor sin escritorio)
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"

# panel.py usa GTK (también lo importa la versión de terminal) y configuracion.py usa YAML
if ! python3 -c 'import gi, yaml; gi.require_version("Gtk", "3.0")' 2>/dev/null; then
    echo "Faltan librerías de Python: sudo apt install python3-gi gir1.2-gtk-3.0 python3-yaml"
    exit 1
fi

# Textual (la interfaz de terminal) en un entorno propio; con los paquetes del sistema para tener GTK y YAML
if [[ ! -x "$DIR/.venv/bin/python" ]]; then
    if ! python3 -m venv --system-site-packages "$DIR/.venv"; then
        rm -rf "$DIR/.venv"
        echo "No se puede crear el entorno de Python: sudo apt install python3-venv"
        exit 1
    fi
fi
"$DIR/.venv/bin/pip" install -q --upgrade textual

mkdir -p "$BIN"
cat > "$BIN/servicios" <<EOF
#!/bin/sh
# Panel de servicios en la terminal (también por SSH). Código: $DIR
exec "$DIR/.venv/bin/python" "$DIR/terminal.py" "\$@"
EOF
chmod +x "$BIN/servicios"
echo "Instalado el comando «servicios» en $BIN"
[[ ":$PATH:" == *":$BIN:"* ]] || echo "Aviso: $BIN no está en el PATH; abre una terminal nueva o añádelo."

if [[ "${1:-}" != "--solo-terminal" ]]; then
    mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
    cp "$DIR/icono.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/panel-servicios.svg"
    cat > "$HOME/.local/share/applications/panel-servicios.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Panel de servicios
Comment=Ver y arrancar o parar OpenClaw, el Creador, Ollama y Docker
Exec=python3 $DIR/panel.py
Icon=panel-servicios
Terminal=false
Categories=System;Monitor;
StartupWMClass=panel-servicios
EOF
    echo "Instalada la app de escritorio «Panel de servicios» en el menú"
fi
