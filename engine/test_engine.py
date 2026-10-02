"""Tests sin red ni GPU: `uv run python test_engine.py`. Textos inventados."""

import engine

lrc = """[ar:Nadie]
[00:00.00]Cancion - Nadie
[00:00.50]作词 : alguien
[00:05.00]primera linea
[00:10.50][00:30.00]estribillo que vuelve
[00:15.00]<00:15.00>por <00:15.40>palabras
[00:20.00]
"""
lines = engine.parse_lrc(lrc)
assert [l["t"] for l in lines] == [5.0, 10.5, 15.0, 20.0, 30.0], lines
assert lines[1]["text"] == "estribillo que vuelve" and lines[0]["end"] == 10.5
assert lines[2]["words"] == [{"t": 15.0, "w": "por"}, {"t": 15.4, "w": "palabras"}]
assert lines[3]["text"] == "" and "end" not in lines[-1]
assert engine.parse_lrc("[00:01.00]\n[00:02.00]") == []  # solo tiempos: no cuenta como letra

assert engine.song_key("Artísta", "Canción (Live)") == "artista-cancion-live"
assert engine.close(181, 180) and not engine.close(190, 180) and engine.close(999, 0)

# Whisper oye "sol" en vez de "son" y se salta "tres"
heard = [{"t": t, "end": t + 0.4, "w": w} for t, w in [(1, "uno"), (2, "dos"), (4, "cuatro"), (6, "cinco"), (7, "sol")]]
out = engine.align(["uno dos tres", "", "Cuatro, cinco son"], heard)
assert [w["t"] for w in out[0]["words"]] == [1, 2, 3.2], out[0]  # "tres" interpolado entre dos y cuatro
assert out[1] == {"t": out[0]["end"], "text": ""}
assert [w["t"] for w in out[2]["words"]] == [4, 6, 7] and out[2]["text"] == "Cuatro, cinco son"
assert all(a["t"] <= b["t"] for a, b in zip(out, out[1:]))
assert [l["t"] for l in engine.align(["a b", "c"], [])] == [0.0, 0.6]  # sin nada oído: no peta

# alineado forzado: "hola" suena en 1.0 s y "yo" en 2.0 s; Whisper decía 0.8 y 1.8. La voz sigue hasta 2.6 s
import numpy as np

em = np.full((200, 29), -10.0, dtype=np.float32)
em[:, 0] = 0
for f, c in [(50, 15), (51, 5), (52, 12), (53, 1), (100, 16), (101, 5)]:  # h o l a / y o (índices de MMS_FA)
    em[f, 0], em[f, c] = -10, 0
rms = np.where((np.arange(200) >= 50) & (np.arange(200) < 130), 0.3, 0.001)
rms[110:118] = 0.001  # respiro corto: no corta la nota
out = engine.refine([{"t": 0.8, "end": 2.0, "text": "Hola, yo", "words": [{"t": 0.8, "end": 1.2, "w": "Hola,"}, {"t": 1.8, "end": 2.0, "w": "yo"}]}, {"t": 3.0, "text": ""}], em, rms)
assert [w["t"] for w in out[0]["words"]] == [1.0, 2.0] and out[0]["t"] == 1.0, out
assert out[0]["end"] == out[0]["words"][-1]["end"] == 2.6 and out[1]["t"] == 2.6, out
print("ok")

# voz en 2-4 s y 10-11 s sobre un fondo 40 dB más bajo (lo que deja la separación): solo esos tramos
import numpy as np

sr = 16000
a = np.full(12 * sr, 0.003, dtype=np.float32)
a[2 * sr : 4 * sr] = a[10 * sr : 11 * sr] = 0.3
clips = engine.voiced(a)
assert len(clips) == 4 and 1.5 <= clips[0] <= 2 and 4 <= clips[1] <= 4.5 and 9.5 <= clips[2] <= 10 and 11 <= clips[3] <= 11.5, clips
assert engine.voiced(np.zeros(sr, dtype=np.float32)) == []
assert engine.JUNK.search("Subtítulos realizados por la comunidad de Amara.org") and engine.JUNK.search("[Música]")
assert not engine.JUNK.search("bailando con la música")

# respiro de 1 s parte la frase; pausa de 6 s deja una línea vacía
w = lambda t, s: {"t": t, "end": t + 0.3, "w": s, "p": 0.9}
out = engine.to_lines([{"words": [w(0, "hola"), w(0.4, "mar"), w(1.7, "otra"), w(8, "vuelta")]}])
assert [l["text"] for l in out] == ["hola mar", "otra", "", "vuelta"], out
assert out[2]["t"] == out[1]["end"] and out[3]["words"][0]["p"] == 0.9
print("ok")

# página de Genius: dos contenedores (los parte un anuncio, no una estrofa: van seguidos), cabecera excluida, [Estribillo] fuera, <br> = salto
page = engine.GeniusPage()
page.feed("""<div>menú</div><div data-lyrics-container="true"><div data-exclude-from-selection="true"><span>3 Contributors</span></div>
[Estribillo]<br/>linea <a href="#"><span>uno</span></a><br/>linea dos</div><div>anuncio</div>
<div data-lyrics-container="true">linea tres<br><img src="x">linea cuatro</div>""")
assert page.text() == "linea uno\nlinea dos\nlinea tres\nlinea cuatro", repr(page.text())
assert engine.same_artist("Joan Jett", "Joan Jett & The Blackhearts") and not engine.same_artist("Avicii", "Joan Jett")
print("ok")

assert engine.same_title("Canción - Remastered 2011", "cancion") and not engine.same_title("Otra", "cancion") and not engine.same_title("Yo soy", "y") and engine.same_title("Canción (Live)", "Cancion")
print("ok")

# YouTube: otra canción que dura lo mismo nunca vale; con título, gana la duración, luego el canal, luego no ser un clip
vids = [{"title": "Otra cosa", "duration": 147, "channel": "Nadie"}, {"title": "Cancion (baile)", "duration": 27, "channel": "Nadie"},
        {"title": "Cancion (Live)", "duration": 260, "channel": "Fulano"}, {"title": "Cancion", "duration": 178, "channel": "Nadie"}]
assert engine.pick_video(vids, "Nadie", "Cancion", 178) is vids[3]
assert engine.pick_video(vids, "Nadie", "Cancion", 147) is vids[3]  # el de 147 s no lleva el título
assert engine.pick_video(vids, "Nadie", "Cancion", 0) is vids[3]  # sin duración: canal y no clip
assert engine.pick_video(vids[:1], "Nadie", "Cancion", 147) is None

# letra vs lo oído: misma canción alta, otra canción ~0
oido = [{"w": w} for w in "uno dos tres cuatro cinco".split()]
assert engine.match_ratio(["uno dos", "tres cuatro cinco"], oido) == 1
assert engine.match_ratio(["sol luna mar", "cielo"], oido) == 0
print("ok")

# estribillo repetido: el segundo escrito va al segundo cantado aunque la estrofa de en medio se oiga mal
heard = [{"t": float(t), "end": t + 0.5, "w": w} for t, w in enumerate("x y z w a b c d x y z w qq rr ss tt x y z w".split())]
out = engine.align(["x y z w", "a b c d", "x y z w", "e f g h", "x y z w"], heard)
assert [l["t"] for l in out] == [0, 4, 8, 12, 16], [l["t"] for l in out]
assert engine.pair(["a", "b"], []) == [None, None] and engine.pair([], ["a"]) == []
print("ok")

# final con ad-lib que no está en la letra y la frase repetida: la línea se queda con la primera vez que suena
heard = [{"t": float(t), "end": t + 0.5, "w": w} for t, w in enumerate("a b c d e f g h oh oh oh oh oh e f g h".split())]
out = engine.align(["a b c d", "e f g h"], heard)
assert [w["t"] for w in out[1]["words"]] == [4, 5, 6, 7], out[1]
print("ok")

# japonés: claves propias (antes todo lo no ASCII se quedaba vacío y chocaba en "cancion"), carácter a carácter
assert engine.song_key("テスト", "猫の歌") == "テスト-猫の歌" and engine.song_key("Artísta", "Canción") == "artista-cancion"
assert engine.norm("Ｔｅｓｔ、") == "test" and engine.norm("が") != engine.norm("か")
assert engine.tokens("猫が歩く hello world、") == ["猫", "が", "歩", "く", "hello", "world", "、"]
assert engine.join_words(["猫", "が", "hello", "歩", "く"]) == "猫が hello 歩く"
assert not engine.same_title("猫の歌", "犬の歌") and engine.same_title("猫の歌", "猫の歌") and not engine.same_title("!!", "??")
# Whisper oye «猫が» como una palabra y «歩く» como otra; la letra escrita sin espacios se sincroniza igual
heard = [{"t": 1.0, "end": 2.0, "w": "猫が"}, {"t": 3.0, "end": 4.0, "w": "歩く"}, {"t": 5.0, "end": 5.5, "w": "。"}]
out = engine.align(["猫が", "歩く。"], heard)
assert [l["t"] for l in out] == [1.0, 3.0] and [w["t"] for w in out[1]["words"]] == [3.0, 3.5, 5.0], out
assert engine.match_ratio(["猫が歩く"], heard) == 1
assert engine.to_lines([{"words": [{"t": 0, "end": 0.3, "w": "猫が"}, {"t": 0.4, "end": 0.7, "w": "歩く"}]}])[0]["text"] == "猫が歩く"
# signos sueltos no casan entre sí
assert engine.pair(engine.keys(["-"], "w"), engine.keys(["、"], "h")) == [0]  # se empareja por posición (sustitución), pero no cuenta como igual
assert engine.match_ratio(["-"], [{"t": 0, "w": "、"}]) == 0
# verso largo (>63 letras = >127 estados): no se desborda
em = np.full((400, 29), -10.0, dtype=np.float32)
em[:, 0] = 0
for i, c in enumerate([1, 2] * 40):
    em[10 + 4 * i, 0], em[10 + 4 * i, c] = -10, 0
assert [a for a, _ in engine.ctc_spans(em, [1, 2] * 40)] == [10 + 4 * i for i in range(80)]
# sincronizar rápido: palabras repartidas por letras hasta la línea siguiente
sp = engine.spread([{"t": 1.0, "text": "ab cd"}, {"t": 2.0, "text": ""}, {"t": None, "text": "x"}])
assert [(w["t"], w["end"]) for w in sp[0]["words"]] == [(1.0, 1.5), (1.5, 2.0)] and sp[0]["end"] == 2.0 and "words" not in sp[1], sp

# historial: keep guarda la actual, restore la recupera
import tempfile
from pathlib import Path

engine.LYRICS = Path(tempfile.mkdtemp())
engine.save({"key": "a-b", "lines": [], "v": 1})
engine.keep("a-b")
engine.save({"key": "a-b", "lines": [], "v": 2})
engine.restore("a", "b")
assert engine.json.loads(engine.doc_path("a-b").read_text())["v"] == 1 and not engine.history("a-b")

# biblioteca: estado de cada letra y su audio; delete audio deja la letra, delete a secas todo
import io
from contextlib import redirect_stdout

engine.AUDIO = Path(tempfile.mkdtemp())
engine.FAILED = engine.AUDIO / "failed"
engine.FAILED.write_text("a-b\nx-y\n")
(engine.AUDIO / "a-b.wav").write_bytes(b"1234")
(engine.AUDIO / "a-bc.wav").write_bytes(b"1")  # otra canción que empieza igual: no es de a-b
engine.save({"key": "a-b", "source": "lrclib", "synced_by": "ia", "lines": [{"t": 1, "text": "x", "words": [{"t": 1, "w": "x", "p": 0.2}]}, {"t": 2, "text": ""}]})


def run(*args):
    with redirect_stdout(io.StringIO()) as out:
        args[0](*args[1:])
    return engine.json.loads(out.getvalue().splitlines()[-1])


lib = run(engine.library)
s = lib["songs"][0]
assert (s["key"], s["lines"], s["timed"], s["doubts"], s["bytes"], s["failed"]) == ("a-b", 1, True, 1, 4, True), s
assert s["audio"] == {"wav": True, "vocals": False, "whisper": False} and lib["failed"] == ["x-y"], lib
run(engine.delete, "a-b", "audio")
assert engine.doc_path("a-b").exists() and not (engine.AUDIO / "a-b.wav").exists() and (engine.AUDIO / "a-bc.wav").exists()
run(engine.delete, "a-b")
assert not engine.doc_path("a-b").exists()
try:
    engine.delete("../x")
    raise AssertionError("delete aceptó una ruta")
except ValueError:
    pass
# texto de Genius con los tiempos de LRCLIB: la línea mal oída se corrige y conserva su tiempo
lrc_lines = [{"t": 10.0, "text": "when you shine you are such a face like the sea"}, {"t": 14.0, "text": "blue moon"}, {"t": 16.0, "text": "come around"}]
engine.genius = lambda *a: "When you shine you are such a rare sight to see\n\nBlue moon, come around"
g = engine.genius_text(lrc_lines, "t", "a", "", 0)
assert [l["text"] for l in g] == ["When you shine you are such a rare sight to see", "", "Blue moon, come around"], g
assert g[0]["t"] == 10.0 and g[2]["t"] == 14.0, g
engine.genius = lambda *a: "otra cancion que no tiene nada que ver"
assert engine.genius_text(lrc_lines, "t", "a", "", 0) is None
engine.genius = lambda *a: "\n".join(l["text"] for l in lrc_lines)  # misma letra: nada que cambiar
assert engine.genius_text(lrc_lines, "t", "a", "", 0) is None
print("ok")
