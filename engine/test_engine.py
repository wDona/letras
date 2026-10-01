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
print("ok")

assert engine.same_title("Canción - Remastered 2011", "cancion") and not engine.same_title("Otra", "cancion") and not engine.same_title("Yo soy", "y") and engine.same_title("Canción (Live)", "Cancion")
print("ok")
