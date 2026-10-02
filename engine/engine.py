"""Motor de Letras. Cada subcomando emite líneas JSON por stdout ({"event": ...}); el último es `done` o `error`.

  find <artista> <título> [álbum] [duración s] [force]   letra: caché, fuentes sincronizadas, fuentes planas
  open <fichero>                                        etiquetas + WAV reproducible de un fichero local
  ai <artista> <título> [audio] [duración s] [lineas]   sincroniza (o transcribe) con IA: voz aislada + Whisper + alineado forzado
                                                        («lineas»: sin Whisper, desde los tiempos por línea que ya hay)
  restore <artista> <título>                            vuelve a la letra de antes de la última sincronización con IA
  prefetch                                              sincroniza con IA las canciones de las playlists de playlists.txt

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
import time
import traceback
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, redirect_stdout
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path

# torch ROCm solo trae kernels gfx1030: las demás RDNA2 (RX 6600 = gfx1032, 6700 = gfx1031…) no salen
# como GPU sin hacerse pasar por gfx1030. Tiene que estar antes de importar torch.
if any(re.search(r"^gfx_target_version 1003(0[1-9]|[1-9]\d)$", p.read_text(), re.M)
       for p in Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties")):
    os.environ.setdefault("HSA_OVERRIDE_GFX_VERSION", "10.3.0")

# la app pasa LETRAS_DATA; a mano, el mismo criterio que la app
DATA = Path(
    os.environ.get("LETRAS_DATA")
    or ("/data/letras" if os.access("/data", os.W_OK) else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "letras")
)
LYRICS = DATA / "lyrics"
AUDIO = DATA / "audio"
# Los modelos de StemLab (~3 GB) se reutilizan si están; su candado de GPU también, para no cargar los dos a la vez.
# Misma carpeta de datos que elige StemLab: STEMLAB_DATA, /data/stemlab o ~/.local/share/stemlab.
_stemlab = Path(
    os.environ.get("STEMLAB_DATA")
    or ("/data/stemlab" if Path("/data/stemlab").is_dir() else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "stemlab")
)
MODELS = Path(os.environ.get("LETRAS_MODELS") or (_stemlab / "models" if _stemlab.is_dir() else DATA / "models"))
GPU_LOCK = MODELS.parent / ".gpu.lock"

VOCAL_MODEL = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
# turbo (4 capas de decoder) oye fatal el canto y rellena con inventos; large-v3 es más lento pero fiable
WHISPER_MODEL = os.environ.get("LETRAS_WHISPER", "large-v3")
UA = "Letras/0.1 (https://github.com/wDona/letras)"  # LRCLIB pide identificarse
BROWSER = {"User-Agent": "Mozilla/5.0"}


def emit(event, **data):
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


# japonés y chino (kana, kanji, signos 「」、。 y katakana de medio ancho): sin espacios, se alinean carácter a carácter
CJK = r"[\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]"


def fold(s):
    """Minúsculas y sin acentos latinos (canción = cancion), pero sin tirar el resto de alfabetos:
    quitar todo lo no ASCII dejaba vacío cualquier texto en japonés. Solo se quitan U+0300-036F, no el
    dakuten de が, y NFKC deja igual el ancho completo y el medio ancho."""
    s = re.sub(r"[\u0300-\u036f]", "", unicodedata.normalize("NFKD", s.casefold()))
    return unicodedata.normalize("NFKC", s)


def slugify(name):
    return re.sub(r"[\W_]+", "-", fold(name)).strip("-") or "cancion"


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
    if not a or not b:  # título de solo signos: no hay con qué comparar
        return False
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


FIREFOX = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"}  # Genius da 403 a un UA pelado


class GeniusPage(HTMLParser):
    """Texto de los <div data-lyrics-container> de una página de Genius: <br> = salto, sin cabeceras ni lo marcado para excluir."""

    def __init__(self):
        super().__init__()
        self.depth = self.skip = 0  # profundidad dentro del contenedor / del trozo excluido
        self.out = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.depth:
            if tag == "br":
                self.out.append("\n")
            elif tag not in VOID:
                self.depth += 1
                if self.skip or a.get("data-exclude-from-selection") == "true":
                    self.skip += 1
        elif tag == "div" and a.get("data-lyrics-container") == "true":
            self.depth = 1
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            self.depth -= 1
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.out.append(data)

    def text(self):
        lines = [l.strip() for l in "".join(self.out).splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(l for l in lines if not re.fullmatch(r"\[.*\]", l))).strip()


VOID = {"br", "img", "hr", "input", "meta", "link", "wbr"}


def same_artist(found, artist):
    a, b = norm(found), norm(re.split(r",|&| feat| ft\.| x ", artist, flags=re.I)[0])
    return bool(a and b) and (a in b or b in a)


def genius(title, artist, album, duration):
    """La que más tiene. Su API de búsqueda pública no pide token; la letra hay que sacarla del HTML."""
    res = get("https://genius.com/api/search/multi", {"q": f"{artist} {title.split(' - ')[0]}"}, {**FIREFOX, "Accept": "application/json"}) or {}
    hits = [h["result"] for sec in res.get("response", {}).get("sections", []) if sec["type"] in ("top_hit", "song") for h in sec["hits"]]
    song = next((s for s in hits if "primary_artist" in s and same_title(s["title"], title) and same_artist(s["primary_artist"]["name"], artist)), None)
    html = song and get(song["url"], headers=FIREFOX, raw=True)
    if not html:
        return None
    page = GeniusPage()
    page.feed(html)
    return page.text() or None


SYNCED = [lrclib, netease, qq, kugou]  # mismo orden que lyrics.sh de quickshell
PLAIN = [lrclib_plain, genius, lyrics_ovh]  # sin tiempos, pero mejor que inventarse la letra con IA


def doc_path(key):
    return LYRICS / f"{key}.json"


def save(doc):
    LYRICS.mkdir(parents=True, exist_ok=True)
    doc_path(doc["key"]).write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    return doc


def history(key):
    """Versiones anteriores (las que pisó la IA), de la más vieja a la más nueva."""
    return sorted((LYRICS / ".history" / key).glob("*.json"))


def keep(key):
    """Guarda la letra actual antes de que la IA la pise: «Volver a la anterior» la recupera."""
    if doc_path(key).exists():
        d = LYRICS / ".history" / key
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{time.time_ns()}.json").write_bytes(doc_path(key).read_bytes())


def restore(artist, title):
    """Vuelve a la versión anterior a la última sincronización con IA (se puede repetir hacia atrás)."""
    key = song_key(artist, title)
    old = history(key)
    if not old:
        raise RuntimeError("no hay versión anterior")
    old[-1].replace(doc_path(key))
    emit("done", key=key, lyrics=json.loads(doc_path(key).read_text()), history=len(old) - 1)


def find(artist, title, album="", duration="0", force=""):
    key = song_key(artist, title)
    LYRICS.mkdir(parents=True, exist_ok=True)  # la app escribe aquí las letras hechas a mano
    if doc_path(key).exists() and not force:
        emit("done", key=key, lyrics=json.loads(doc_path(key).read_text()), cached=True, history=len(history(key)))
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


def pick_video(entries, artist, title, duration):
    """De los resultados de YouTube, uno que lleve el título; entre ellos, el de la misma duración (±3 s), luego el del
    canal de la artista, luego el que no sea un clip. Ninguno con el título -> None: otra canción que dure lo mismo
    no vale, sincronizar la letra con otra canción es inventarse los tiempos."""
    t = norm(title.split(" - ")[0])
    named = [e for e in entries if t in norm(e.get("title") or "")]
    if not named:
        return None
    timed = lambda e: bool(duration) and close(e.get("duration") or 0, duration)
    # max se queda con el primero en empate: el orden de YouTube desempata
    return max(named, key=lambda e: (timed(e), same_artist(e.get("channel") or "", artist), (e.get("duration") or 0) >= 60))


def download(artist, title, dst_stem, duration):
    from yt_dlp import YoutubeDL

    opts = {"format": "bestaudio/best", "outtmpl": f"{dst_stem}.%(ext)s", "quiet": True, "noplaylist": True, "noprogress": True}
    with redirect_stdout(sys.stderr), YoutubeDL({**opts, "extract_flat": "in_playlist"}) as ydl:
        # «Artista - Título audio» tapaba la canción con otras del mismo canal; así sale la primera
        entries = ydl.extract_info(f"ytsearch5:{artist} {title.split(' - ')[0]}", download=False)["entries"]
    video = pick_video(entries, artist, title, duration)
    if not video:
        raise RuntimeError("no encuentro la canción en YouTube: abre el fichero en la app")
    with redirect_stdout(sys.stderr), YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video["url"], download=True)
        return Path(ydl.prepare_filename(info))


def separate_vocals(wav, out):
    from audio_separator.separator import Separator

    tmp = out.parent / f".{out.stem}"
    tmp.mkdir(exist_ok=True)
    with gpu_lock(), redirect_stdout(sys.stderr):
        # el YAML del modelo pide 4 pasadas por trozo (calidad StemLab); para que Whisper oiga la voz sobran 2 y es la mitad de GPU
        # torch.compile de los bloques del RoFormer + autocast (fp16 donde no estropea): −43 % en una RX 6600 (217 -> 124 s
        # con la caché de compilación hecha). La voz difiere 73 dB por debajo de la señal, nada para Whisper ni el alineado.
        sep = Separator(model_file_dir=str(MODELS), output_dir=str(tmp), output_format="WAV", log_level=40, output_single_stem="Vocals", use_torch_compile=True, use_autocast=True, mdxc_params={"segment_size": 256, "override_model_segment_size": False, "batch_size": None, "overlap": 2, "pitch_shift": 0})
        sep.load_model(model_filename=VOCAL_MODEL)
        files = [tmp / Path(f).name for f in sep.separate(str(wav))]
        del sep
        import torch

        torch.cuda.empty_cache()  # 8 GB de VRAM: si no, Whisper se carga con la de la separación aún pillada
    for f in files:
        if "(Vocals)" in f.name:
            f.replace(out)
        else:
            f.unlink()
    tmp.rmdir()


def voiced(audio, sr=16000, frame=0.25, gap=1.5, pad=0.3):
    """Tramos con voz de la pista de voz aislada -> [ini, fin, ini, fin…] (s) para clip_timestamps.
    Whisper se inventa frases en silencios e instrumentales; si no los oye, no puede.
    Voz = menos de 24 dB por debajo de lo que suena fuerte (percentil 95): la separación deja algo de música."""
    import numpy as np

    n = int(sr * frame)
    rms = np.sqrt(np.mean(audio[: len(audio) // n * n].reshape(-1, n) ** 2, axis=1))
    if not len(rms) or rms.max() < 1e-4:
        return []
    on = rms > max(np.percentile(rms, 95) * 0.06, 1e-4)
    spans = []
    for k in np.flatnonzero(on):
        a, b = k * frame - pad, (k + 1) * frame + pad
        if spans and a - spans[-1][1] < gap:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    end = len(audio) / sr
    return [round(min(max(t, 0.0), end), 2) for s in spans for t in s]


# lo que Whisper suelta en silencios (aprendió de subtítulos de YouTube) o como etiqueta de música
JUNK = re.compile(r"amara\.org|subt[ií]tulos (realizados|por)|gracias por ver|thanks? (you )?for watching|^\W*(m[uú]sica|music|aplausos|applause)?\W*$", re.I)


def whisper_words(vocals):
    """Whisper sobre la voz aislada -> segmentos [{"t", "end", "words": [{"t", "end", "w", "p"}]}].
    `p` = confianza de Whisper en la palabra (0-1), para marcar lo dudoso en el editor."""
    import torch
    import whisper

    audio = whisper.load_audio(str(vocals))
    clips = voiced(audio)
    if not clips:
        return []
    with gpu_lock(), redirect_stdout(sys.stderr):
        device = "cuda" if torch.cuda.is_available() else "cpu"
        # load_model deja los pesos en fp32 (large-v3 = 6 GB, no cabe en 8 GB con el escritorio): en fp16 son 3 GB.
        # Se carga en CPU y se sube ya convertido, para no tener nunca el pico fp32 en la GPU.
        model = whisper.load_model(WHISPER_MODEL, device="cpu", download_root=str(MODELS / "whisper"))
        if device == "cuda":
            model = model.half()
            for m in model.modules():  # su LayerNorm pasa la entrada a fp32: sus pesos (poca cosa) también
                if isinstance(m, torch.nn.LayerNorm):
                    m.float()
            model = model.to(device)
        r = model.transcribe(
            audio,
            word_timestamps=True,
            condition_on_previous_text=False,  # si no, un error se arrastra (y se repite) el resto de la canción
            clip_timestamps=clips,
            hallucination_silence_threshold=2,
            # sin reintentos a más temperatura: los trozos que salen raros ya se tiran abajo (JUNK, compresión, logprob).
            # Medido en 3 canciones: Whisper 60 s en vez de 76-122 s, y lo que oye se parece igual o más a la letra
            temperature=0.0,
        )
        del model
        torch.cuda.empty_cache()
    segs = []
    for s in r["segments"]:
        # bucles («la la la la…» x40) y segmentos que el propio Whisper ve como ruido
        if JUNK.search(s["text"].strip()) or s["compression_ratio"] > 2.4 or s["avg_logprob"] < -1:
            continue
        words = [
            {"t": round(w["start"], 3), "end": round(w["end"], 3), "w": w["word"].strip(), "p": round(w["probability"], 2)}
            for w in s["words"]
            if w["word"].strip()
        ]
        if words:
            segs.append({"t": words[0]["t"], "end": words[-1]["end"], "words": words})
    return segs


def to_lines(segs, gap=0.6, most=10, stanza=4):
    """Segmentos de Whisper (frases largas) -> versos: corta en cada respiro, y deja una línea vacía en las pausas largas."""
    lines, cur = [], []

    def flush():
        if cur:
            lines.append({"t": cur[0]["t"], "end": cur[-1]["end"], "text": join_words(w["w"] for w in cur), "words": cur[:]})
            cur.clear()

    for w in (w for s in segs for w in s["words"]):
        if cur and (w["t"] - cur[-1]["end"] > gap or len(cur) >= most):
            flush()
        if not cur and lines and w["t"] - lines[-1]["end"] > stanza:
            lines.append({"t": lines[-1]["end"], "text": ""})
        cur.append(w)
    flush()
    return lines


def norm(w):
    return re.sub(r"[\W_]", "", fold(w))


def tokens(text):
    """Palabras por espacios, y cada carácter japonés/chino suelto (no llevan espacios entre palabras)."""
    return re.findall(rf"{CJK}|(?:(?!{CJK})\S)+", text)


def join_words(ws):
    """Lo contrario de tokens: espacio entre palabras, nada entre dos caracteres japoneses."""
    out = ""
    for w in ws:
        out += ("" if not out or (re.match(CJK, out[-1]) and re.match(CJK, w[0])) else " ") + w
    return out


def keys(ws, side):
    """Claves para `pair`: la palabra normalizada. Un signo suelto («、», «-») normaliza a "" y no debe
    casar con otro signo cualquiera: cada lado lleva una clave que nunca coincide con el otro."""
    return [norm(w) or f"\0{side}" for w in ws]


def split_heard(heard):
    """Palabras de Whisper -> tokens como los de la letra; un trozo japonés reparte su tiempo entre sus caracteres."""
    out = []
    for h in heard:
        ts = tokens(h["w"])
        t0 = h.get("t", 0.0)
        end = h.get("end", t0)
        for k, w in enumerate(ts):
            a, b = t0 + (end - t0) * k / len(ts), t0 + (end - t0) * (k + 1) / len(ts)
            out.append({**h, "t": round(a, 3), "end": round(b, 3), "w": w})
    return out


def pair(a, b):
    """Alineado global de palabras (distancia de edición): a[i] -> índice en b, o None si Whisper no la oyó.
    SequenceMatcher empareja primero el bloque igual más largo: con el estribillo repetido podía casar el
    primero escrito con el segundo cantado y dejar media canción sin líneas. Esto minimiza los cambios en
    toda la canción, así que cada estribillo cae en el suyo. Una palabra mal oída se empareja con lo que sonó ahí."""
    n, m = len(a), len(b)
    D = [list(range(m + 1))] + [[i] + [0] * m for i in range(1, n + 1)]
    for i in range(1, n + 1):
        Di, Dp, ai = D[i], D[i - 1], a[i - 1]
        for j in range(1, m + 1):
            Di[j] = min(Dp[j - 1] + (ai != b[j - 1]), Dp[j] + 1, Di[j - 1] + 1)
    # Hacia atrás desde el final, en empate se salta antes lo oído que sobra: así cada palabra escrita se queda
    # con la primera vez que suena, y las repeticiones y ad-libs que no están en la letra quedan después.
    out, i, j = [None] * n, n, m
    while i and j:
        if D[i][j] == D[i][j - 1] + 1:
            j -= 1
        elif D[i][j] == D[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            out[i - 1] = j - 1
            i, j = i - 1, j - 1
        else:
            i -= 1
    return out


def align(texts, heard):
    """Reparte los tiempos de las palabras oídas por Whisper sobre la letra escrita (una cadena por línea).
    Cada palabra escrita toma el tiempo de la oída con la que la empareja `pair`, y lo que quede sin
    tiempo se interpola entre vecinos."""
    written = [(i, w) for i, t in enumerate(texts) for w in tokens(t)]
    heard = split_heard(heard)
    times = [None] * len(written)
    for k, j in enumerate(pair(keys([w for _, w in written], "w"), keys([h["w"] for h in heard], "h"))):
        if j is not None:
            times[k] = [heard[j]["t"], heard[j]["end"]]
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
    for k in range(1, len(times)):  # por si acaso: nunca hacia atrás
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


def match_ratio(texts, heard):
    """Parte de la letra escrita que Whisper oyó tal cual y en orden. Bien sincronizada ronda 0.4-0.8; ~0 = otra canción."""
    written = keys([w for t in texts for w in tokens(t)], "w")
    said = keys([h["w"] for h in split_heard(heard)], "h")
    return sum(j is not None and written[k] == said[j] for k, j in enumerate(pair(written, said))) / max(1, len(written))


FPS = 50  # tramas por segundo de wav2vec2 (salto de 320 muestras a 16 kHz)


def ctc_frames(vocals, out):
    """Voz aislada -> (log-probs de MMS_FA por trama [n, 29], volumen por trama [n]), cacheados en `out` (.npz).
    MMS_FA reconoce letras (a-z, ', * = cualquier otra cosa): con la letra conocida, el alineado forzado
    encuentra dónde empieza cada una. Mucho más fino que los tiempos de Whisper, que salen de su atención."""
    import numpy as np

    if out.exists():
        d = np.load(out)
        return d["em"], d["rms"]
    import torch
    import whisper
    from torchaudio.pipelines import MMS_FA

    audio = whisper.load_audio(str(vocals))
    hop, n = 16000 // FPS, len(audio) // (16000 // FPS)
    rms = np.sqrt(np.mean(audio[: n * hop].reshape(n, hop) ** 2, axis=1))
    torch.hub.set_dir(str(MODELS / "torch"))
    em = np.full((n, len(MMS_FA.get_labels())), -1e4, dtype=np.float32)
    em[:, 0] = 0  # si el modelo devuelve alguna trama de menos al final de un trozo: blanco
    with gpu_lock(), redirect_stdout(sys.stderr), torch.inference_mode():
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = MMS_FA.get_model().to(device)
        # a trozos de 30 s (la atención de la canción entera no cabe) con 1 s de contexto a cada lado
        chunk, ctx = 30 * FPS, FPS
        for a in range(0, n, chunk):
            s = max(0, a - ctx)
            x = torch.from_numpy(audio[s * hop : (a + chunk + ctx) * hop]).to(device)[None]
            e = torch.log_softmax(model(x)[0][0], -1).float().cpu().numpy()[a - s :][: min(chunk, n - a)]
            em[a : a + len(e)] = e
        del model
        torch.cuda.empty_cache()
    np.savez(out, em=em, rms=rms)
    return em, rms


def ctc_spans(em, targets):
    """Alineado forzado de CTC (Viterbi): em [tramas, letras] en log-prob, targets sin blancos ->
    [(trama de inicio, trama de fin exclusiva)] de cada target. El de torchaudio (deprecado) daba caminos peores."""
    import numpy as np

    lab = np.zeros(2 * len(targets) + 1, dtype=int)  # blanco, t0, blanco, t1… blanco
    lab[1::2] = targets
    S, T = len(lab), len(em)
    skip = np.zeros(S, dtype=bool)  # saltarse el blanco entre dos letras distintas
    skip[3::2] = lab[3::2] != lab[1:-2:2]
    dp = np.full(S, -np.inf)
    dp[:2] = em[0, lab[:2]]
    back = np.zeros((T, S), dtype=np.int8)
    for t in range(1, T):
        prev = np.stack([dp, np.r_[-np.inf, dp[:-1]], np.where(skip, np.r_[-np.inf, -np.inf, dp[:-2]], -np.inf)])
        back[t] = prev.argmax(0)
        dp = prev[back[t], np.arange(S)] + em[t, lab]
    s = S - 1 if dp[-1] >= dp[-2] else S - 2
    spans = [[T, 0] for _ in targets]
    for t in range(T - 1, -1, -1):
        if s % 2:
            sp = spans[s // 2]
            sp[0], sp[1] = t, max(sp[1], t + 1)
        s -= int(back[t, s])  # int: con el int8 de numpy, s se desbordaba pasados 127 estados
    return spans


def voice(rms):
    """Tramas con voz: menos de 24 dB por debajo de lo fuerte (el mismo criterio que `voiced`)."""
    import numpy as np

    return rms > max(np.percentile(rms, 95) * 0.06, 1e-4) if len(rms) else rms > 0


def shifted(lines, by):
    """Copia con todos los tiempos + `by` s."""
    out = json.loads(json.dumps(lines))
    for line in out:
        for o in [line, *line.get("words", [])]:
            for k in ("t", "end"):
                if o.get(k) is not None:
                    o[k] = round(max(0.0, o[k] + by), 3)
    return out


# El audio de YouTube puede ser otra versión que la tuya (el videoclip, con su intro): los tiempos por línea que ya
# traía la letra (LRCLIB los casa con la duración de tu canción, o los pusiste tú) dicen cuánto se corre.
def version_shift(ref, lines):
    """Desfase (s) de `lines` (sacadas del audio) respecto a `ref` (misma letra, tiempos de tu versión); 0 si no hay con qué comparar."""
    import numpy as np

    d = [b["t"] - a["t"] for a, b in zip(ref, lines) if a.get("t") is not None and b.get("t") is not None and a["text"]]
    return float(np.median(d)) if len(ref) == len(lines) and len(d) >= 3 else 0.0


def refine(lines, em, rms, pad=1.0, far=1.0, breath=0.3):
    """Afina los tiempos (de Whisper o de `align`) con alineado forzado, línea a línea:
    - cada palabra empieza cuando suena su primera letra;
    - la línea (y su última palabra) acaba cuando la voz calla de verdad, no cuando Whisper cree: así la nota
      sostenida sigue iluminándose y la pausa (los puntos de la pantalla) solo sale en silencio.
    Ventana = la línea ±`pad` s; los `*` de los bordes se comen lo que cante la línea de al lado.
    Una palabra que se iría a más de `far` s de donde estaba se queda como estaba (alineado perdido).
    Palabras sin letras latinas (japonés, signos) no se tocan."""
    import numpy as np
    from torchaudio.pipelines import MMS_FA

    labels = MMS_FA.get_dict()
    star, n = labels["*"], len(em)
    loud = voice(rms)
    for i, line in enumerate(lines):
        ws = line.get("words")
        if not ws:
            continue
        a = max(0, int((line["t"] - pad) * FPS))
        b = min(n, int((line["end"] + pad) * FPS))
        chars = [[labels[c] for c in fold(w["w"]) if c in labels and c not in "-*"] for w in ws]
        targets = [star] + [c for cs in chars for c in cs] + [star]
        if len(targets) == 2 or b - a < 2 * len(targets):  # nada que alinear, o ventana imposible
            continue
        spans = ctc_spans(em[a:b], targets)
        k = 1
        for w, cs in zip(ws, chars):
            if not cs:
                continue
            t0, t1 = (a + spans[k][0]) / FPS, (a + spans[k + len(cs) - 1][1]) / FPS
            k += len(cs)
            if abs(t0 - w["t"]) <= far:
                w["t"], w["end"] = round(t0, 3), round(max(t1, t0 + 0.05), 3)
        for p, q in zip(ws, ws[1:]):  # nunca hacia atrás
            q["t"] = max(q["t"], p["t"])
        line["t"] = ws[0]["t"]
    # fin = mientras siga sonando voz desde la última palabra, sin pisar la línea siguiente
    starts = [l["t"] for l in lines if l.get("words")] + [n / FPS]
    k = 0
    for line in lines:
        ws = line.get("words")
        if not ws:
            continue
        k += 1
        nxt = starts[k]
        f = last = int(ws[-1]["t"] * FPS)
        while f < len(loud) and f / FPS < nxt and f - last < breath * FPS:  # un respiro corto no corta la nota
            if loud[f]:
                last = f + 1
            f += 1
        end = round(min(max(ws[-1]["end"], last / FPS), nxt), 3)
        ws[-1]["end"] = line["end"] = max(end, ws[-1]["t"])
    for p, line in zip(lines, lines[1:]):  # línea en blanco (pausa): justo tras la anterior
        if not line["text"] and "end" in p:
            line["t"] = p["end"]
    return lines


def get_audio(artist, title, audio="", duration="0"):
    """Audio de la canción -> audio/<clave>.wav: del fichero local si lo hay, si no de YouTube."""
    key = song_key(artist, title)
    wav = AUDIO / f"{key}.wav"
    AUDIO.mkdir(parents=True, exist_ok=True)
    src = urllib.parse.unquote(urllib.parse.urlparse(audio).path) if audio.startswith("file://") else audio
    if src and Path(src).is_file():
        to_wav(src, wav)
    else:
        # ponytail: de 5 resultados el de misma duración; otra versión con intro distinta la arregla el desfase de la UI
        got = download(artist, title, AUDIO / f".{key}.dl", float(duration or 0))
        to_wav(got, wav)
        got.unlink()


def spread(lines):
    """Líneas con tiempo (LRC de internet) -> palabras repartidas por letras hasta la línea siguiente:
    el punto de partida del alineado cuando no se pasa Whisper."""
    out = []
    for i, line in enumerate(lines):
        if line["t"] is None or not line["text"]:
            out.append(dict(line))
            continue
        t, nxt = line["t"], next((l["t"] for l in lines[i + 1 :] if l["t"] is not None), line["t"] + 5)
        ws = tokens(line["text"])
        total, acc, words = sum(len(w) + 1 for w in ws), 0, []
        for w in ws:
            a = t + (nxt - t) * acc / total
            acc += len(w) + 1
            words.append({"t": round(a, 3), "end": round(t + (nxt - t) * acc / total, 3), "w": w})
        out.append({**line, "end": nxt, "words": words})
    return out


def ai(artist, title, audio="", duration="0", mode=""):
    """Sincroniza con IA. Si ya hay texto (de internet o corregido a mano) lo respeta y solo pone tiempos;
    si no, la letra es la transcripción. Cada paso se cachea: repetirlo tras editar el texto es instantáneo.
    mode="lineas": sin Whisper, parte de los tiempos por línea que ya tiene la letra (más rápido; sin la
    comprobación de que el audio sea la canción). Antes de guardar, la versión actual va al historial."""
    key = song_key(artist, title)
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav, vocals, heard_f = AUDIO / f"{key}.wav", AUDIO / f"{key}.vocals.wav", AUDIO / f"{key}.{WHISPER_MODEL}.words.json"
    doc = json.loads(doc_path(key).read_text()) if doc_path(key).exists() else {}
    fast = mode == "lineas"
    if fast and not any(l["t"] is not None and l["text"] for l in doc.get("lines", [])):
        raise RuntimeError("la letra no tiene tiempos por línea: usa «Sincronizar con IA»")
    steps = ["Consiguiendo el audio", "Separando la voz", "Transcribiendo con Whisper", "Alineando", "Ajustando cada palabra"]
    if fast:
        steps[2:4] = []

    def progress(i):
        emit("progress", step=i + 1, total=len(steps), label=steps[i])

    if not wav.exists():
        progress(0)
        get_audio(artist, title, audio, duration)
    if not vocals.exists():
        progress(1)
        separate_vocals(wav, vocals)
    if fast:
        progress(2)
        em, rms = ctc_frames(vocals, AUDIO / f"{key}.ctc.npz")
        # sin Whisper no hay forma fiable de medir el desfase de otra versión (la voz sola encajaba mal en 4 de 17):
        # si el audio no dura lo que tu canción, es otra versión y los tiempos por línea no le valen
        if float(duration or 0) and not close(len(rms) / FPS, float(duration)):
            raise RuntimeError(f"el audio dura {len(rms) / FPS:.0f} s y tu canción {float(duration):.0f} s: es otra versión. Usa «Sincronizar con IA»")
        lines = refine(spread(doc["lines"]), em, rms, pad=0.5, far=float("inf"))
        keep(key)
        doc.update(synced=True, synced_by="ia", lines=lines)
        emit("done", key=key, lyrics=save(doc), history=len(history(key)))
        return
    if not heard_f.exists():
        progress(2)
        heard_f.write_text(json.dumps(whisper_words(vocals), ensure_ascii=False))
    segs = json.loads(heard_f.read_text())
    progress(3)
    texts = [l["text"] for l in doc.get("lines", [])]
    heard = [w for s in segs for w in s["words"]]
    if any(texts) and heard and match_ratio(texts, heard) < 0.15:
        for f in (wav, vocals, heard_f):  # el audio es el equivocado: que el próximo intento lo busque otra vez
            f.unlink(missing_ok=True)
        raise RuntimeError("lo que se oye no se parece a la letra: el audio era otra canción. Prueba otra vez o abre el fichero")
    if any(texts):
        lines = align(texts, heard)
    else:
        lines = to_lines(segs)
        doc.update(source="ia", edited=False)  # transcrita: sin revisar
    if lines:
        progress(4)
        lines = refine(lines, *ctc_frames(vocals, AUDIO / f"{key}.ctc.npz"))
        by = version_shift(doc.get("lines", []), lines)
        if abs(by) > 1:  # otra versión del audio: a los tiempos de la tuya
            lines = shifted(lines, -by)
    keep(key)
    doc.update(key=key, artist=artist, title=title, synced=True, synced_by="ia", instrumental=not lines, lines=lines)
    emit("done", key=key, lyrics=save(doc), history=len(history(key)))


PLAYLISTS = DATA / "playlists.txt"


def playlist_tracks(url):
    """Playlist (o álbum) pública de Spotify -> [(artista, título, duración s)], de su página para incrustar:
    sin cuenta ni API (la de Spotify pide Premium desde 2026). Solo públicas; la página trae hasta ~100."""
    kind, pid = re.search(r"(playlist|album)[/:]([A-Za-z0-9]+)", url).groups()
    html = get(f"https://open.spotify.com/embed/{kind}/{pid}", headers=BROWSER, raw=True)
    data = json.loads(re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S).group(1))
    tracks = data["props"]["pageProps"]["state"]["data"]["entity"]["trackList"]
    return [(t["subtitle"].replace("\u00a0", " "), t["title"], t["duration"] / 1000) for t in tracks]


# Juego abierto (Steam/Proton, gamescope, Wine): la IA en segundo plano no toca la GPU. LETRAS_GAMES lo cambia.
GAMES = re.compile(os.environ.get("LETRAS_GAMES", r"SteamLaunch AppId=|gamescope|wineserver|\.exe\b"), re.I)


def gaming():
    for f in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            if GAMES.search(f.read_bytes().replace(b"\0", b" ").decode(errors="ignore")):
                return True
        except OSError:
            pass  # el proceso acabó mientras se miraba
    return False


def prefetch():
    """Deja sincronizadas con IA las canciones de las playlists de playlists.txt (una URL por línea, # comenta).
    Pensado para un timer de systemd: salta lo ya hecho, y mientras la GPU trabaja con una canción ya se
    descarga la siguiente. Con un juego abierto espera; si el juego empieza a mitad de canción la corta
    (cada canción va en su propio proceso: al cortarla la VRAM se libera entera) y la repite después.
    Lo que falla se apunta en .prefetch-failed y no se reintenta (bórralo para reintentar)."""
    from concurrent.futures import ThreadPoolExecutor

    urls = [l.split("#")[0].strip() for l in PLAYLISTS.read_text().splitlines()] if PLAYLISTS.exists() else []
    failed_f = DATA / ".prefetch-failed"
    failed = set(failed_f.read_text().split()) if failed_f.exists() else set()
    todo, seen = [], set()
    for url in filter(None, urls):
        try:
            tracks = playlist_tracks(url)
        except Exception as e:
            print(f"playlist {url}: {e}", file=sys.stderr)
            continue
        for artist, title, dur in tracks:
            key = song_key(artist, title)
            doc = json.loads(doc_path(key).read_text()) if doc_path(key).exists() else {}
            if key in seen or key in failed or doc.get("synced_by") == "ia" or doc.get("instrumental"):
                continue
            seen.add(key)
            todo.append((artist, title, dur))
    print(f"prefetch: {len(todo)} canciones por sincronizar", file=sys.stderr)

    def fetch(t):  # en otro hilo: red y ffmpeg, nada de GPU
        if not (AUDIO / f"{song_key(*t[:2])}.wav").exists():
            get_audio(t[0], t[1], "", str(t[2]))

    def wait_game():
        if gaming():
            print("prefetch: juego abierto, en pausa", file=sys.stderr)
            while gaming():
                time.sleep(30)

    with ThreadPoolExecutor(1) as pool:
        nxt = pool.submit(fetch, todo[0]) if todo else None
        for i, (artist, title, dur) in enumerate(todo):
            key = song_key(artist, title)
            try:
                nxt.result()
            except Exception:
                pass  # ai lo reintenta y, si vuelve a fallar, cuenta como fallo
            nxt = pool.submit(fetch, todo[i + 1]) if i + 1 < len(todo) else None
            with redirect_stdout(sys.stderr):  # sus eventos son para la app; aquí, al log
                find(artist, title, "", str(round(dur)))
            doc = json.loads(doc_path(key).read_text()) if doc_path(key).exists() else {}
            # letra con tiempos por línea (LRCLIB…): sin Whisper, ~40 % más rápido y respeta esas líneas (medido en
            # 3 canciones: 83-89 % de líneas a <0,5 s de las de LRCLIB, con Whisper 52-73 %). Si el audio es otra
            # versión, el modo rápido se niega y se repite con Whisper, que mide y corrige el desfase.
            modes = ["lineas", ""] if any(l.get("t") is not None and l.get("text") for l in doc.get("lines", [])) else [""]
            while True:
                wait_game()
                p = subprocess.Popen([sys.executable, __file__, "ai", artist, title, "", str(round(dur)), modes[0]], stdout=sys.stderr)
                while p.poll() is None and not gaming():
                    time.sleep(5)
                if p.returncode is None:  # empezó un juego: fuera de la GPU ya, se repite luego (lo hecho queda en caché)
                    p.terminate()
                    p.wait()
                    continue
                if p.returncode and len(modes) > 1:
                    modes.pop(0)
                    continue
                break
            if p.returncode == 0:
                print(f"prefetch: {key} ok", file=sys.stderr)
            else:
                failed.add(key)
                failed_f.write_text("\n".join(sorted(failed)) + "\n")
                print(f"prefetch: {key} falló (código {p.returncode}, el error está arriba)", file=sys.stderr)


def main():
    cmd, *args = sys.argv[1:] or ["help"]
    commands = {"find": find, "open": open_file, "ai": ai, "restore": restore, "prefetch": prefetch}
    try:
        if cmd not in commands:
            raise ValueError(f"subcomando desconocido: {cmd}")
        commands[cmd](*args)
    except Exception as e:
        traceback.print_exc()  # a stderr: la app solo enseña el mensaje
        emit("error", message=f"{type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
