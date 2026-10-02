#!/usr/bin/env bash
# Instalador de Letras para Linux: compila la app y la añade al lanzador de aplicaciones
# (rofi drun, quickshell, GNOME, KDE…: cualquiera que lea ~/.local/share/applications).
#
#   ./install.sh                 detecta la GPU (NVIDIA / AMD / ninguna) e instala
#   ./install.sh --gpu cuda      fuerza torch para NVIDIA   (también: rocm, cpu)
#   ./install.sh --uninstall     quita app, icono y entrada del menú (tus letras no se tocan)
#
# No usa sudo: si falta algo del sistema, dice qué instalar y para.
# La carpeta del repo tiene que quedarse donde está: el motor (engine/) se ejecuta desde aquí.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/letras"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
DESKTOP="$APPS/letras.desktop"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

GPU=""
case "${1:-}" in
  --uninstall)
    systemctl --user disable --now letras-prefetch.timer 2>/dev/null || true
    rm -f "$UNITS/letras-prefetch.service" "$UNITS/letras-prefetch.timer"
    systemctl --user daemon-reload 2>/dev/null || true
    rm -f "$BIN" "$DESKTOP" "$ICONS/128x128/apps/letras.png"
    command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
    say "Letras desinstalado. Tus letras siguen en su carpeta de datos."
    exit 0 ;;
  --gpu) GPU="${2:-}"; [[ "$GPU" =~ ^(cuda|rocm|cpu)$ ]] || die "--gpu tiene que ser cuda, rocm o cpu" ;;
  "") ;;
  *) die "opción desconocida: $1 (usa --gpu cuda|rocm|cpu o --uninstall)" ;;
esac

# --- 1. dependencias del sistema ---------------------------------------------------------------
distro="$(. /etc/os-release 2>/dev/null; echo "${ID:-} ${ID_LIKE:-}")"
hint() {  # paquete para Arch / Debian-Ubuntu / Fedora
  case "$distro" in
    *arch*) echo "$1" ;;
    *debian*|*ubuntu*) echo "$2" ;;
    *fedora*|*rhel*) echo "$3" ;;
    *) echo "$1 (Arch) / $2 (Debian) / $3 (Fedora)" ;;
  esac
}
missing=()
command -v uv >/dev/null || missing+=("uv  →  curl -LsSf https://astral.sh/uv/install.sh | sh")
command -v npm >/dev/null || missing+=("Node.js + npm  →  $(hint nodejs\ npm nodejs\ npm nodejs\ npm)")
command -v cargo >/dev/null || missing+=("Rust  →  $(hint rust 'rustup (https://rustup.rs)' rust\ cargo)")
command -v ffmpeg >/dev/null || missing+=("ffmpeg  →  $(hint ffmpeg ffmpeg ffmpeg-free)")
pkg-config --exists webkit2gtk-4.1 2>/dev/null ||
  missing+=("WebKitGTK 4.1  →  $(hint webkit2gtk-4.1 libwebkit2gtk-4.1-dev webkit2gtk4.1-devel)")
# sin gst-plugins-good el WebView no tiene salida de audio ni lee WAV
{ command -v gst-inspect-1.0 >/dev/null && gst-inspect-1.0 autoaudiosink >/dev/null 2>&1 && gst-inspect-1.0 wavparse >/dev/null 2>&1; } ||
  missing+=("GStreamer good plugins  →  $(hint gst-plugins-good gstreamer1.0-plugins-good gstreamer1-plugins-good)")
if ((${#missing[@]})); then
  printf '\033[1;31mFaltan dependencias del sistema:\033[0m\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  [[ "$distro" == *debian* || "$distro" == *ubuntu* ]] &&
    echo "  (Debian/Ubuntu: además build-essential libssl-dev librsvg2-dev libayatana-appindicator3-dev)" >&2
  exit 1
fi

# --- 2. GPU -------------------------------------------------------------------------------------
if [[ -z "$GPU" ]]; then
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; then GPU=cuda
  elif [[ -e /dev/kfd ]] && grep -qs 0x1002 /sys/class/drm/card*/device/vendor; then GPU=rocm
  else GPU=cpu; fi
fi
case "$GPU" in
  cuda) say "GPU NVIDIA: torch con CUDA" ;;
  rocm) say "GPU AMD: torch con ROCm" ;;
  cpu)  say "Sin GPU compatible: todo en CPU (funciona, pero la IA tarda bastante más)" ;;
esac

# --- 3. motor Python ----------------------------------------------------------------------------
say "Instalando el motor (Python + modelos de IA, varios GB la primera vez)…"
(cd "$REPO/engine" && uv sync --frozen)
if [[ "$GPU" != rocm ]]; then
  # pyproject trae torch ROCm; aquí se cambia por el de esta máquina. La app ejecuta .venv/bin/python
  # directamente, así que un `uv sync` posterior lo devolvería a ROCm: en ese caso, repite este script.
  index="https://download.pytorch.org/whl/$([[ $GPU == cuda ]] && echo cu128 || echo cpu)"
  uv pip install --python "$REPO/engine/.venv/bin/python" --reinstall torch torchaudio torchvision --index-url "$index"
fi
"$REPO/engine/.venv/bin/python" "$REPO/engine/test_engine.py" >/dev/null 2>&1 || die "el motor no pasa sus tests (engine/test_engine.py)"

# --- 4. app -------------------------------------------------------------------------------------
say "Compilando la app (unos minutos la primera vez)…"
cd "$REPO/app"
if [[ -d node_modules ]]; then npm install --no-audit --no-fund; else npm ci --no-audit --no-fund; fi
npm run tauri build -- --no-bundle

# --- 5. instalar --------------------------------------------------------------------------------
install -Dm755 "$REPO/app/src-tauri/target/release/letras" "$BIN"
install -Dm644 "$REPO/app/src-tauri/icons/128x128.png" "$ICONS/128x128/apps/letras.png"

exec_line="$BIN"
# WebKitGTK + driver NVIDIA: sin esto la ventana puede salir en blanco
[[ "$GPU" == cuda ]] && exec_line="env WEBKIT_DISABLE_DMABUF_RENDERER=1 $BIN"

mkdir -p "$APPS"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Letras
GenericName=Letras sincronizadas
Comment=Letras sincronizadas con efectos, de internet o con IA
Exec=$exec_line
Icon=letras
Terminal=false
Categories=AudioVideo;Audio;Music;
Keywords=lyrics;letras;karaoke;lrc;spotify;
StartupWMClass=letras
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true

# --- 6. segundo plano: sincroniza con IA las playlists de playlists.txt cada 6 h ------------------
if command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  mkdir -p "$UNITS"
  cat > "$UNITS/letras-prefetch.service" <<EOF
[Unit]
Description=Letras: sincroniza con IA las canciones de tus playlists (playlists.txt de la carpeta de datos)

[Service]
Type=oneshot
ExecStart=$REPO/engine/.venv/bin/python $REPO/engine/engine.py prefetch
# Horas de GPU con una playlist nueva. Comparte el candado de GPU con la app; con un juego abierto espera.
TimeoutStartSec=infinity
Nice=19
CPUSchedulingPolicy=idle
IOSchedulingClass=idle
EOF
  cat > "$UNITS/letras-prefetch.timer" <<EOF
[Unit]
Description=Letras: mira cada 6 h si hay canciones nuevas en tus playlists

[Timer]
OnStartupSec=15min
OnUnitInactiveSec=6h

[Install]
WantedBy=timers.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now letras-prefetch.timer >/dev/null 2>&1
fi

data="${LETRAS_DATA:-$([[ -w /data ]] && echo /data/letras || echo "${XDG_DATA_HOME:-$HOME/.local/share}/letras")}"
say "Listo. Búscala como «Letras» en tu lanzador, o ejecuta: letras"
echo "    Datos (letras, audio): $data   (cámbialo con la variable LETRAS_DATA)"
echo "    Segundo plano: pon URLs de playlists públicas de Spotify en $data/playlists.txt (una por línea)"
echo "    Motor: $REPO/engine   (no muevas la carpeta del repo; para reinstalar, vuelve a ejecutar ./install.sh)"
[[ ":$PATH:" == *":$HOME/.local/bin:"* ]] || echo "    Nota: ~/.local/bin no está en tu PATH; desde el lanzador funciona igual."
