import { Channel, invoke } from "@tauri-apps/api/core";

export type Word = { t: number; end?: number; w: string; p?: number }; // p: confianza de Whisper (0-1)
export type Line = { t: number | null; end?: number; text: string; words?: Word[] };
export type Lyrics = {
  key: string;
  artist: string;
  title: string;
  source: string; // de dónde sale el texto: lrclib, netease, qq, kugou, lrclib_plain, genius, lyrics_ovh, ia, manual
  times_from?: string; // de dónde salen los tiempos por línea, si no es `source` (texto de Genius + tiempos de LRCLIB)
  synced: boolean;
  synced_by?: "ia";
  ai_mode?: "whisper" | "lineas";
  instrumental: boolean;
  edited?: boolean;
  offset?: number; // s, desfase de esta canción (otra versión, intro distinta)
  hidden?: boolean; // no sale en el escritorio (lyrics.sh)
  note?: string;
  duration?: number;
  lines: Line[];
};
/** Resumen de una letra guardada (`engine.py library`). */
export type Song = Pick<Lyrics, "key" | "artist" | "title" | "source" | "times_from" | "synced_by" | "ai_mode" | "edited" | "instrumental" | "offset" | "hidden" | "note" | "duration"> & {
  lines: number; // con texto
  timed: boolean;
  words: boolean;
  doubts: number; // líneas con palabras dudosas de Whisper
  history: number;
  modified: number; // s epoch
  audio: { wav: boolean; vocals: boolean; whisper: boolean };
  bytes: number;
  failed: boolean; // el prefetch la dio por fallida
};
export type Track = { artist: string; title: string; album: string; duration: number; url?: string; art?: string };

export type EngineEvent =
  | { event: "progress"; step: number; total: number; label: string }
  | { event: "pct"; value: number }
  | { event: "waiting"; label: string }
  | { event: "done"; [k: string]: unknown }
  | { event: "error"; message: string };

/** Lanza un subcomando de engine.py. Resuelve con el evento `done`; `id` sirve para cancelarlo. */
export function engine<T>(
  args: string[],
  onEvent: (e: EngineEvent) => void = () => {},
  id: string = crypto.randomUUID(),
): Promise<T> {
  const channel = new Channel<EngineEvent>();
  channel.onmessage = onEvent;
  return invoke<T>("engine", { id, args, onEvent: channel });
}

/** Escribe en la terminal de la app (stderr de Rust). */
export const log = (msg: string) => void invoke("log", { msg });

/** JSON de la carpeta de datos; `fallback` si no existe o está roto. */
export async function readJson<T>(path: string, fallback: T): Promise<T> {
  try {
    return { ...fallback, ...JSON.parse(new TextDecoder().decode(await invoke<ArrayBuffer>("read_file", { path }))) };
  } catch {
    return fallback;
  }
}

export const writeText = (path: string, text: string) =>
  invoke("write_file", new TextEncoder().encode(text), { headers: { path: encodeURIComponent(path) } })
    .catch((e) => log(`guardar ${path}: ${e}`));
export const writeJson = (path: string, data: unknown) => writeText(path, JSON.stringify(data, null, 1));

/** Texto de la carpeta de datos; "" si no existe. */
export const readText = (path: string) =>
  invoke<ArrayBuffer>("read_file", { path }).then((b) => new TextDecoder().decode(b), () => "");

export const paths = { data: "" };
export async function initPaths() {
  paths.data = await invoke<string>("data_dir_cmd");
}
export const dataPath = (name: string) => `${paths.data}/${name}`;
export const lyricsPath = (key: string) => `${paths.data}/lyrics/${key}.json`;

export const SOURCE_LABEL: Record<string, string> = {
  lrclib: "LRCLIB",
  netease: "NetEase",
  qq: "QQ Música",
  kugou: "Kugou",
  lrclib_plain: "LRCLIB (sin tiempos)",
  genius: "Genius (sin tiempos)",
  lyrics_ovh: "lyrics.ovh (sin tiempos)",
  ia: "IA",
  manual: "a mano",
};
/** Fuentes que ya traen tiempos por línea. */
export const SYNCED_SOURCES = new Set(["lrclib", "netease", "qq", "kugou"]);

/** Palabras que Whisper no tiene claras: lo primero que revisar en una letra transcrita. */
export const doubtful = (l: Line) => (l.words ?? []).filter((w) => w.p != null && w.p < 0.5).map((w) => w.w);

/** 83.456 -> "1:23.45" */
export const fmtTime = (s: number | null) =>
  s == null ? "" : `${Math.floor(s / 60)}:${(s % 60).toFixed(2).padStart(5, "0")}`;
/** "1:23.45" o "83.4" -> segundos; null si vacío o roto. */
export function parseTime(v: string): number | null {
  const m = v.trim().match(/^(?:(\d+):)?(\d+(?:\.\d+)?)$/);
  return m ? Math.round(((+(m[1] ?? 0)) * 60 + +m[2]) * 1000) / 1000 : null;
}
