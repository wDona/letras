"""Motor de Letras. Cada subcomando emite líneas JSON por stdout ({"event": ...}); el último es `done` o `error`.

  find <artista> <título> [álbum] [duración s] [force]   letra: caché, fuentes sincronizadas, fuentes planas
  open <fichero>                                        etiquetas + WAV reproducible de un fichero local
  ai <artista> <título> [audio]                         sincroniza (o transcribe) con IA: voz aislada + Whisper

Documento de letra (lyrics/<clave>.json):
  {"key", "artist", "title", "source", "synced": bool, "synced_by"?: "ia", "instrumental": bool,
   "lines": [{"t": s|None, "end"?: s, "text": str, "words"?: [{"t": s, "end"?: s, "w": str}]}]}
"""

import base64
import fcntl
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, redirect_stdout
from difflib import SequenceMatcher
from pathlib import Path

# la app pasa LETRAS_DATA; a mano, el mismo criterio que la app
DATA = Path(
    os.environ.get("LETRAS_DATA")
    or ("/data/letras" if os.access("/data", os.W_OK) else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "letras")
)
LYRICS = DATA / "lyrics"
AUDIO = DATA / "audio"
# Los modelos de StemLab (~3 GB) se reutilizan si están; su candado de GPU también, para no cargar los dos a la vez.
_stemlab = Path("/data/stemlab/models")
MODELS = Path(os.environ.get("LETRAS_MODELS") or (_stemlab if _stemlab.is_dir() else DATA / "models"))
GPU_LOCK = MODELS.parent / ".gpu.lock"

VOCAL_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
UA = "Letras/0.1 (https://github.com/wDona/letras)"  # LRCLIB pide identificarse
BROWSER = {"User-Agent": "Mozilla/5.0"}


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def slugify(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "cancion"


def song_key(artist, title):
    return slugify(f"{artist}-{title}")


def to_wav(src, dst):
    """Cualquier cosa que lea ffmpeg -> WAV PCM 16-bit estéreo 44.1 kHz (WebKitGTK no decodifica FLAC)."""
    tmp = Path(dst).with_name(f".{Path(dst).stem}.tmp.wav")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", str(tmp)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg no pudo leer {Path(src).name}: {r.stderr.strip().splitlines()[-1:]}")
    tmp.replace(dst)


@contextmanager
def gpu_lock():
    """Una sola carga de IA en la GPU a la vez, compartido con StemLab. El kernel lo suelta si el proceso muere."""
    GPU_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(GPU_LOCK, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit("waiting", label="Esperando a la GPU")
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


# ---------------------------------------------------------------- letras de internet

TS = r"(\d+):(\d+(?:\.\d+)?)"
# créditos que NetEase, QQ y Kugou meten al principio: "作词 : ...", "Lyrics by：..."
CREDIT = re.compile(r"^\s*[一-鿿]+\s*[:：]|^[^:：]{1,30}：")


def parse_lrc(text):
    """LRC (también el «enhanced» con <mm:ss.xx> por palabra) -> líneas ordenadas por tiempo.
    Una línea con varias marcas ([00:10.00][01:20.00]estribillo) sale una vez por marca."""
    sec = lambda m, s: round(int(m) * 60 + float(s), 3)
    lines = []
    for raw in text.splitlines():
        stamps = re.findall(rf"\[{TS}\]", raw)
        if not stamps:
            continue  # metadatos tipo [ar:...] o basura
        body = re.sub(rf"\[{TS}\]", "", raw)
        words = [{"t": sec(m, s), "w": w.strip()} for m, s, w in re.findall(rf"<{TS}>([^<]*)", body) if w.strip()]
        line_text = re.sub(r"\s+", " ", re.sub(rf"<{TS}>", "", body)).strip()
        if CREDIT.search(line_text):
            continue
        for m, s in stamps:
            t = sec(m, s)
            if t == 0 and " - " in line_text:
                continue  # "Título - Artista" en el segundo 0
            line = {"t": t, "text": line_text}
            if words:
                line["words"] = words
            lines.append(line)
    lines.sort(key=lambda l: l["t"])
    for a, b in zip(lines, lines[1:]):
        a.setdefault("end", b["t"])
    return lines if any(l["text"] for l in lines) else []


def plain_lines(text):
    return [{"t": None, "text": l.strip()} for l in text.strip().splitlines()]


def get(url, params=None, headers=None, raw=False):
    """GET -> JSON (o texto con raw). None si falla: una fuente caída no tumba a las demás."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {"User-Agent": UA}), timeout=8) as r:
            body = r.read().decode("utf-8", "replace")
        return body if raw else json.loads(body)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"{url.split('?')[0]}: {e}", file=sys.stderr)
        return None


def close(d, duration):
    """Las búsquedas de las fuentes chinas son difusas: solo vale la misma duración (±3 s)."""
    return not duration or abs(d - duration) <= 3


def same_title(found, title):
    """Las búsquedas devuelven cualquier cosa parecida: el título tiene que contener al otro (sin «- Remastered»)."""
    a, b = norm(found.split(" - ")[0]), norm(title.split(" - ")[0])
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


# Cada fuente: (título, artista, álbum, duración s) -> texto LRC o plano, o None.
# Del título se quita el " - Remastered 2011" y similares de Spotify.


def lrclib_records(title, artist, album, duration):
    """/get es exacto (±2 s); /search encuentra la misma canción en otro álbum (recopilatorios, remasters)."""
    params = {"track_name": title, "artist_name": artist, "album_name": album, "duration": round(duration)}
    exact = get("https://lrclib.net/api/get", {k: v for k, v in params.items() if v})
    found = [r for r in get("https://lrclib.net/api/search", {"track_name": title, "artist_name": artist}) or [] if same_title(r["trackName"], title)]
    return [exact, *sorted(found, key=lambda r: not close(r.get("duration") or 0, duration))] if exact else found


def lrclib(title, artist, album, duration):
    return next((r["syncedLyrics"] for r in lrclib_records(title, artist, album, duration) if r.get("syncedLyrics")), None)


def netease(title, artist, album, duration):
    hdr = {**BROWSER, "Referer": "https://music.163.com"}
    res = get("https://music.163.com/api/search/get", {"s": f"{title.split(' - ')[0]} {artist}", "type": 1, "limit": 10}, hdr) or {}
    song = next((s for s in (res.get("result") or {}).get("songs", []) if close(s["duration"] / 1000, duration) and same_title(s["name"], title)), None)
    lrc = song and get(f"https://music.163.com/api/song/lyric?id={song['id']}&lv=1", headers=hdr)
    return lrc and (lrc.get("lrc") or {}).get("lyric")


def qq(title, artist, album, duration):
    hdr = {**BROWSER, "Referer": "https://y.qq.com"}
    res = get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp", {"w": f"{title.split(' - ')[0]} {artist}", "format": "json", "n": 10}, hdr) or {}
    song = next((s for s in res.get("data", {}).get("song", {}).get("list", []) if close(s["interval"], duration) and same_title(s["songname"], title)), None)
    lrc = song and get(
        "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
        {"songmid": song["songmid"], "format": "json", "nobase64": 1, "g_tk": 5381},
        hdr,
    )
    return lrc and lrc.get("lyric")


def kugou(title, artist, album, duration):
    res = get(
        "https://krcs.kugou.com/search",
        {"ver": 1, "man": "yes", "client": "mobi", "keyword": f"{artist} - {title.split(' - ')[0]}", "duration": round(duration * 1000), "hash": ""},
        BROWSER,
    ) or {}
    c = next((c for c in res.get("candidates", []) if close(c["duration"] / 1000, duration) and same_title(c["song"], title)), None)
    lrc = c and get(f"https://krcs.kugou.com/download?ver=1&client=pc&fmt=lrc&charset=utf8&id={c['id']}&accesskey={c['accesskey']}", headers=BROWSER)
    return lrc and lrc.get("content") and base64.b64decode(lrc["content"]).decode("utf-8", "replace")


def lrclib_plain(title, artist, album, duration):
    return next((r["plainLyrics"] for r in lrclib_records(title, artist, album, duration) if r.get("plainLyrics")), None)


def lyrics_ovh(title, artist, album, duration):
    q = lambda s: urllib.parse.quote(s, safe="")
    res = get(f"https://api.lyrics.ovh/v1/{q(artist)}/{q(title.split(' - ')[0])}")
    return res and res.get("lyrics")


SYNCED = [lrclib, netease, qq, kugou]  # mismo orden que lyrics.sh de quickshell
PLAIN = [lrclib_plain, lyrics_ovh]  # sin tiempos, pero mejor que inventarse la letra con IA


def doc_path(key):
    return LYRICS / f"{key}.json"


def save(doc):
    LYRICS.mkdir(parents=True, exist_ok=True)
    doc_path(doc["key"]).write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    return doc


def find(artist, title, album="", duration="0", force=""):
    key = song_key(artist, title)
    LYRICS.mkdir(parents=True, exist_ok=True)  # la app escribe aquí las letras hechas a mano
    if doc_path(key).exists() and not force:
        emit("done", key=key, lyrics=json.loads(doc_path(key).read_text()), cached=True)
        return
    args = (title, artist, album, float(duration or 0))
    sources = SYNCED + PLAIN
    for i, src in enumerate(sources):
        emit("progress", step=i + 1, total=len(sources), label=f"Buscando en {src.__name__}")
        text = src(*args)
        lines = text and (parse_lrc(text) if src in SYNCED else plain_lines(text))
        if lines:
            doc = {"key": key, "artist": artist, "title": title, "source": src.__name__, "synced": src in SYNCED, "instrumental": False, "lines": lines}
            emit("done", key=key, lyrics=save(doc), cached=False)
            return
    emit("done", key=key, lyrics=None, cached=False)


# ---------------------------------------------------------------- fichero local


def open_file(path):
    path = Path(path).resolve()
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    fmt = json.loads(r.stdout or "{}").get("format", {})
    tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
    artist, title = tags.get("artist", ""), tags.get("title", "")
    if not title:  # sin etiquetas: «Artista - Título.mp3»
        artist, _, title = path.stem.rpartition(" - ")
    key = song_key(artist, title)
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav = AUDIO / f"{key}.wav"
    if not wav.exists():
        emit("progress", step=1, total=1, label="Convirtiendo")
        to_wav(path, wav)
    emit("done", key=key, artist=artist, title=title, album=tags.get("album", ""), duration=float(fmt.get("duration") or 0), wav=str(wav))


# ---------------------------------------------------------------- IA


def download(query, dst_stem):
    from yt_dlp import YoutubeDL

    opts = {"format": "bestaudio/best", "outtmpl": f"{dst_stem}.%(ext)s", "quiet": True, "noplaylist": True, "noprogress": True}
    with redirect_stdout(sys.stderr), YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=True)["entries"][0]
        return Path(ydl.prepare_filename(info))


def separate_vocals(wav, out):
    from audio_separator.separator import Separator

    tmp = out.parent / f".{out.stem}"
    tmp.mkdir(exist_ok=True)
    with gpu_lock(), redirect_stdout(sys.stderr):
        sep = Separator(model_file_dir=str(MODELS), output_dir=str(tmp), output_format="WAV", log_level=40)
        sep.load_model(model_filename=VOCAL_MODEL)
        files = [tmp / Path(f).name for f in sep.separate(str(wav))]
    for f in files:
        if "(Vocals)" in f.name:
            f.replace(out)
        else:
            f.unlink()
    tmp.rmdir()


def whisper_words(vocals):
    """Whisper sobre la voz aislada -> segmentos [{"t", "end", "words": [{"t", "end", "w"}]}]."""
    import torch
    import whisper

    with gpu_lock(), redirect_stdout(sys.stderr):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = whisper.load_model("turbo", device=device, download_root=str(MODELS / "whisper"))
        r = model.transcribe(str(vocals), word_timestamps=True, condition_on_previous_text=False)
        del model
        torch.cuda.empty_cache()
    segs = []
    for s in r["segments"]:
        words = [{"t": round(w["start"], 3), "end": round(w["end"], 3), "w": w["word"].strip()} for w in s["words"] if w["word"].strip()]
        if words:
            segs.append({"t": words[0]["t"], "end": words[-1]["end"], "words": words})
    return segs


def norm(w):
    w = unicodedata.normalize("NFKD", w.lower()).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", w)


def align(texts, heard):
    """Reparte los tiempos de las palabras oídas por Whisper sobre la letra escrita (una cadena por línea).
    Las coincidencias exactas mandan; donde Whisper oyó otra cosa se reparte en proporción, y lo que
    quede sin tiempo se interpola entre vecinos."""
    written = [(i, w) for i, t in enumerate(texts) for w in t.split()]
    times = [None] * len(written)
    if heard:
        sm = SequenceMatcher(None, [norm(w) for _, w in written], [norm(h["w"]) for h in heard], autojunk=False)
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op in ("equal", "replace"):
                for k in range(i1, i2):
                    h = heard[j1 + (k - i1) * (j2 - j1) // (i2 - i1)]
                    times[k] = [h["t"], h["end"]]
    known = [k for k, t in enumerate(times) if t]
    for k, t in enumerate(times):
        if t:
            continue
        prev = max((j for j in known if j < k), default=None)
        nxt = min((j for j in known if j > k), default=None)
        if prev is not None and nxt is not None:
            a, b = times[prev][1], times[nxt][0]
            t0 = a + (b - a) * (k - prev) / (nxt - prev)
        elif prev is not None:
            t0 = times[prev][1] + 0.3 * (k - prev)
        elif nxt is not None:
            t0 = max(0.0, times[nxt][0] - 0.3 * (nxt - k))
        else:
            t0 = 0.3 * k
        times[k] = [round(t0, 3), round(t0 + 0.25, 3)]
    for k in range(1, len(times)):  # el reparto proporcional puede repetir palabra: nunca hacia atrás
        times[k][0] = max(times[k][0], times[k - 1][0])
    lines, k = [], 0
    for i, text in enumerate(texts):
        words = []
        while k < len(written) and written[k][0] == i:
            words.append({"t": times[k][0], "end": times[k][1], "w": written[k][1]})
            k += 1
        if words:
            lines.append({"t": words[0]["t"], "end": words[-1]["end"], "text": text, "words": words})
        else:  # línea en blanco: pausa justo tras la anterior
            lines.append({"t": lines[-1]["end"] if lines else 0.0, "text": ""})
    return lines


def ai(artist, title, audio=""):
    """Sincroniza con IA. Si ya hay texto (de internet o corregido a mano) lo respeta y solo pone tiempos;
    si no, la letra es la transcripción. Cada paso se cachea: repetirlo tras editar el texto es instantáneo."""
    key = song_key(artist, title)
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav, vocals, heard_f = AUDIO / f"{key}.wav", AUDIO / f"{key}.vocals.wav", AUDIO / f"{key}.words.json"
    steps = ["Consiguiendo el audio", "Separando la voz", "Transcribiendo con Whisper", "Alineando"]

    def progress(i):
        emit("progress", step=i + 1, total=len(steps), label=steps[i])

    if not wav.exists():
        progress(0)
        src = urllib.parse.unquote(urllib.parse.urlparse(audio).path) if audio.startswith("file://") else audio
        if src and Path(src).is_file():
            to_wav(src, wav)
        else:
            # ponytail: primer resultado de YouTube; si es otra versión (intro distinta), lo arregla el desfase de la UI
            got = download(f"{artist} - {title} audio", AUDIO / f".{key}.dl")
            to_wav(got, wav)
            got.unlink()
    if not vocals.exists():
        progress(1)
        separate_vocals(wav, vocals)
    if not heard_f.exists():
        progress(2)
        heard_f.write_text(json.dumps(whisper_words(vocals), ensure_ascii=False))
    segs = json.loads(heard_f.read_text())
    progress(3)
    doc = json.loads(doc_path(key).read_text()) if doc_path(key).exists() else {}
    texts = [l["text"] for l in doc.get("lines", [])]
    if any(texts):
        lines = align(texts, [w for s in segs for w in s["words"]])
    else:
        lines = [{"t": s["t"], "end": s["end"], "text": " ".join(w["w"] for w in s["words"]), "words": s["words"]} for s in segs]
        doc["source"] = "ia"
    doc.update(key=key, artist=artist, title=title, synced=True, synced_by="ia", instrumental=False, lines=lines)
    emit("done", key=key, lyrics=save(doc))


def main():
    cmd, *args = sys.argv[1:] or ["help"]
    commands = {"find": find, "open": open_file, "ai": ai}
    try:
        if cmd not in commands:
            raise ValueError(f"subcomando desconocido: {cmd}")
        commands[cmd](*args)
    except Exception as e:
        emit("error", message=f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
