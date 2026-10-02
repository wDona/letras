"""Motor de Letras. Cada subcomando emite líneas JSON por stdout ({"event": ...}); el último es `done` o `error`.

  find <artista> <título> [álbum] [duración s] [force]   letra: caché, fuentes sincronizadas, fuentes planas
  open <fichero>                                        etiquetas + WAV reproducible de un fichero local
  ai <artista> <título> [audio] [duración s]            sincroniza (o transcribe) con IA: voz aislada + Whisper

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
        sep = Separator(model_file_dir=str(MODELS), output_dir=str(tmp), output_format="WAV", log_level=40, mdxc_params={"segment_size": 256, "override_model_segment_size": False, "batch_size": None, "overlap": 2, "pitch_shift": 0})
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


def ai(artist, title, audio="", duration="0"):
    """Sincroniza con IA. Si ya hay texto (de internet o corregido a mano) lo respeta y solo pone tiempos;
    si no, la letra es la transcripción. Cada paso se cachea: repetirlo tras editar el texto es instantáneo."""
    key = song_key(artist, title)
    AUDIO.mkdir(parents=True, exist_ok=True)
    wav, vocals, heard_f = AUDIO / f"{key}.wav", AUDIO / f"{key}.vocals.wav", AUDIO / f"{key}.{WHISPER_MODEL}.words.json"
    steps = ["Consiguiendo el audio", "Separando la voz", "Transcribiendo con Whisper", "Alineando"]

    def progress(i):
        emit("progress", step=i + 1, total=len(steps), label=steps[i])

    if not wav.exists():
        progress(0)
        src = urllib.parse.unquote(urllib.parse.urlparse(audio).path) if audio.startswith("file://") else audio
        if src and Path(src).is_file():
            to_wav(src, wav)
        else:
            # ponytail: de 5 resultados el de misma duración; otra versión con intro distinta la arregla el desfase de la UI
            got = download(artist, title, AUDIO / f".{key}.dl", float(duration or 0))
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
    doc.update(key=key, artist=artist, title=title, synced=True, synced_by="ia", instrumental=not lines, lines=lines)
    emit("done", key=key, lyrics=save(doc))


def main():
    cmd, *args = sys.argv[1:] or ["help"]
    commands = {"find": find, "open": open_file, "ai": ai}
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
