# Letras

Gestor de letras sincronizadas de lo que suena en Spotify. App Tauri + Svelte; el motor Python sale de StemLab.
La app no enseña la letra: la consigue, la sincroniza y la gestiona. La enseña el escritorio (dotfiles:
`eww/scripts/lyrics.sh`, que usa la de aquí antes que la de internet → lanzador SUPER, brb SUPER+N, otros monitores).

- **Fuente**: solo Spotify (`playerctl -p spotify`); el navegador y demás reproductores no cuentan.
- **Biblioteca**: todas las letras guardadas con de dónde salió el texto (LRCLIB, Genius, IA…), cómo está sincronizada
  (venía sincronizada / IA con Whisper / IA rápida sobre los tiempos que traía / a mano / sin tiempos), dudas de Whisper,
  qué audio hay en caché y cuánto ocupa. Filtros, búsqueda (`/`), orden y acciones en bloque sobre la selección (casillas, shift-clic para un tramo, Ctrl+A, Esc):
  IA, IA rápida, buscar otra vez, volver a la anterior, ocultar/mostrar, instrumental, quitar desfase, reintentar en
  prefetch, borrar audio o las canciones.
- **Por canción**: editar (E), sincronizar con IA o rápida, transcribir, buscar otra vez (la anterior va al historial),
  volver a la anterior, desfase, ocultar en el escritorio, instrumental, nota, borrar audio o la canción entera.
- **Playlists**: editar `playlists.txt`, lanzar el prefetch ya, ver y reintentar las fallidas.
- **Búsqueda** (`engine.py find`), en orden, hasta que una dé letra:
  1. Sincronizadas: LRCLIB, NetEase, QQ Música, Kugou (las mismas que `lyrics.sh` de quickshell; título igual y duración ±3 s).
  2. Sin tiempos: Genius (su búsqueda pública y el HTML de la página), LRCLIB (plana), lyrics.ovh.
  3. Nada: la transcribe la IA.
  Si la sincronizada la tiene también Genius y dice otra cosa (las de LRCLIB las sube cualquiera y a veces están mal oídas),
  se queda el texto de Genius con los tiempos de la sincronizada, palabra a palabra (`genius_text`). `engine.py retext` lo hace
  con las ya guardadas.
- **Siempre sincronizada**: si lo encontrado no trae tiempos (o no hay nada), la IA arranca sola al acabar la búsqueda:
  a la letra plana le pone tiempos, y si no hay letra la transcribe. Un trabajo de IA a la vez; si cambias de canción
  mientras trabaja, el resultado se guarda para la suya y se mira si la que suena ahora necesita otro.
- **IA** (`engine.py ai`): audio del fichero/URL local o, si no hay, primer resultado de YouTube (`yt-dlp`) →
  voz aislada (BS-RoFormer) → Whisper large-v3 (`LETRAS_WHISPER` para otro) con tiempos por palabra, solo sobre los tramos
  donde hay voz (en silencios e instrumentales se inventaba frases) y tirando bucles y «subtítulos de Amara.org». Si ya hay texto (de internet o corregido a
  mano) solo le pone tiempos; si no, la letra es la transcripción. Después, alineado forzado (MMS_FA, letra a letra) sobre la voz
  aislada: cada palabra empieza cuando suena, y cada verso acaba cuando la voz calla de verdad (los puntos de pausa solo salen en silencio). Cada paso se cachea en `audio/`: tras corregir
  el texto, «Sincronizar con IA» es instantáneo.
- **Volver atrás**: antes de que la IA pise una letra, la versión anterior va a `lyrics/.history/<clave>/`;
  «Volver a la anterior» (editor) la recupera, y se puede repetir hacia atrás (`engine.py restore`).
- **Sincronizar rápido** (editor): sin Whisper, parte de los tiempos por línea que ya trae la letra (LRCLIB…) y solo
  ajusta cada palabra. Ahorra el paso de Whisper, pero no comprueba que el audio de YouTube sea esa canción.
- **En segundo plano** (`engine.py prefetch`): sincroniza con IA las canciones de las playlists públicas de Spotify
  listadas en `playlists.txt` (carpeta de datos, una URL por línea). Salta lo ya hecho y descarga la siguiente
  mientras la GPU trabaja. Si la letra ya trae tiempos por línea usa el modo rápido (sin Whisper; si el audio
  es otra versión, repite con Whisper). Con un juego abierto (Steam/Proton, gamescope, Wine; `LETRAS_GAMES` para otro patrón)
  espera, y si el juego empieza a mitad de canción la corta para soltar la VRAM y la repite luego. Lo que falla va a
  `.prefetch-failed`. Lo lanza cada 6 h un timer de systemd de usuario (`letras-prefetch.timer`) que pone `install.sh`
  (y quita `--uninstall`).
- **Editor** (E): al transcribir con IA se abre solo y marca en amarillo las líneas con palabras dudosas. Texto y tiempo por línea, pegar el texto entero, desplazar todo, tap-sync (Espacio en cada
  línea mientras suena) y re-sincronizar con IA.

Datos: `$LETRAS_DATA`, si no `/data/letras` si `/data` es escribible, si no `~/.local/share/letras`.
Modelos: los de StemLab (`/data/stemlab/models`) si existen, y su mismo candado de GPU: nunca cargan los dos a la vez.

## Desarrollo

```sh
cd engine && uv sync
cd ../app && npm install && npm run tauri dev   # puerto 1430
```

Tests: `cd engine && uv run python test_engine.py` y `cd app/src-tauri && cargo test`.
Instalar en el lanzador: `./install.sh`.
