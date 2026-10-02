# Letras

Letra de lo que suene, sincronizada y con efectos. App Tauri + Svelte; el motor Python sale de StemLab.

- **Fuente**: lo que suene por MPRIS (Spotify, Firefox, mpv… vía `playerctl`) o un fichero local abierto en la app.
  En el navegador, «Artista - Canción (Official Video)» se limpia antes de buscar.
- **Búsqueda** (`engine.py find`), en orden, hasta que una dé letra:
  1. Sincronizadas: LRCLIB, NetEase, QQ Música, Kugou (las mismas que `lyrics.sh` de quickshell; título igual y duración ±3 s).
  2. Sin tiempos: LRCLIB (plana), Genius (su búsqueda pública y el HTML de la página), lyrics.ovh.
  3. Nada: la transcribe la IA.
- **Siempre sincronizada**: si lo encontrado no trae tiempos (o no hay nada), la IA arranca sola al acabar la búsqueda:
  a la letra plana le pone tiempos, y si no hay letra la transcribe. Un trabajo de IA a la vez; si cambias de canción
  mientras trabaja, el resultado se guarda para la suya y se mira si la que suena ahora necesita otro.
- **IA** (`engine.py ai`): audio del fichero/URL local o, si no hay, primer resultado de YouTube (`yt-dlp`) →
  voz aislada (BS-RoFormer) → Whisper large-v3 (`LETRAS_WHISPER` para otro) con tiempos por palabra, solo sobre los tramos
  donde hay voz (en silencios e instrumentales se inventaba frases) y tirando bucles y «subtítulos de Amara.org». Si ya hay texto (de internet o corregido a
  mano) solo le pone tiempos; si no, la letra es la transcripción. Cada paso se cachea en `audio/`: tras corregir
  el texto, «Sincronizar con IA» es instantáneo.
- **Editor** (E): al transcribir con IA se abre solo y marca en amarillo las líneas con palabras dudosas. Texto y tiempo por línea, pegar el texto entero, desplazar todo, tap-sync (Espacio en cada
  línea mientras suena) y re-sincronizar con IA.
- **Estilo** (S): presets (Apple Music, Karaoke, Neón, Vapor, Minimal) y todo ajustable: fuente, tamaños,
  colores, resplandor, barrido karaoke por palabra o línea, desenfoque, zoom, velocidad, fondo aurora / carátula /
  color / transparente. Se guarda en `settings.json` de la carpeta de datos (pensado para leerlo desde quickshell).
- Atajos: `[` `]` desfase de la canción ±0.1 s, F pantalla completa, Espacio play/pausa (modo fichero),
  clic en una línea para saltar ahí.

Datos: `$LETRAS_DATA`, si no `/data/letras` si `/data` es escribible, si no `~/.local/share/letras`.
Modelos: los de StemLab (`/data/stemlab/models`) si existen, y su mismo candado de GPU: nunca cargan los dos a la vez.

## Desarrollo

```sh
cd engine && uv sync
cd ../app && npm install && npm run tauri dev   # puerto 1430
```

Tests: `cd engine && uv run python test_engine.py` y `cd app/src-tauri && cargo test`.
Instalar en el lanzador: `./install.sh`.
