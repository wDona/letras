export type Settings = {
  preset: string;
  font: string;
  size: number; // px de la línea activa
  weight: number;
  align: "left" | "center";
  lineGap: number; // em
  uppercase: boolean;
  sung: string; // color de lo ya cantado
  unsung: string;
  glowColor: string;
  glow: number; // px de resplandor en la línea activa
  blur: number; // px de desenfoque por línea de distancia
  scale: number; // escala de la línea activa
  lift: number; // px que sube la palabra que suena
  fill: "word" | "line" | "none"; // barrido karaoke por palabra, línea entera o nada
  speed: number; // ms del desplazamiento entre líneas
  anchor: number; // 0..1, altura de la línea activa en la ventana
  flicker: boolean; // parpadeo de neón
  bg: "aurora" | "cover" | "solid" | "transparent";
  c1: string;
  c2: string;
  c3: string;
  dim: number; // 0..1, oscurecer el fondo
  offset: number; // s, desfase global (Bluetooth, etc.)
};

export const DEFAULTS: Settings = {
  preset: "Apple Music",
  font: "Inter, system-ui, sans-serif",
  size: 44,
  weight: 800,
  align: "left",
  lineGap: 0.55,
  uppercase: false,
  sung: "#ffffff",
  unsung: "rgba(255,255,255,0.35)",
  glowColor: "#ffffff",
  glow: 14,
  blur: 1.6,
  scale: 1.0,
  lift: 2,
  fill: "word",
  speed: 650,
  anchor: 0.38,
  flicker: false,
  bg: "aurora",
  c1: "#ff3d7f",
  c2: "#7a3dff",
  c3: "#00c2ff",
  dim: 0.35,
  offset: 0,
};

/** Puntos de partida; luego todo se puede tocar a mano. */
export const PRESETS: Record<string, Partial<Settings>> = {
  "Apple Music": { ...DEFAULTS },
  Karaoke: {
    align: "center", fill: "word", sung: "#ffe14d", unsung: "#ffffff", glowColor: "#ffb800", glow: 18,
    blur: 0, scale: 1.08, lift: 4, weight: 900, bg: "cover", dim: 0.55, uppercase: false,
  },
  Neón: {
    align: "center", fill: "line", sung: "#ffffff", unsung: "rgba(255,80,220,0.3)", glowColor: "#ff2bd6", glow: 28,
    blur: 2.5, scale: 1.12, lift: 0, weight: 700, flicker: true, bg: "aurora", c1: "#ff2bd6", c2: "#2b0a4a", c3: "#00f0ff",
    dim: 0.55, uppercase: true, font: "'Bebas Neue', Oswald, Impact, sans-serif",
  },
  Vapor: {
    align: "center", fill: "word", sung: "#9ef6ff", unsung: "rgba(255,190,240,0.4)", glowColor: "#ff71ce", glow: 22,
    blur: 1.2, scale: 1.05, lift: 3, weight: 800, bg: "aurora", c1: "#ff71ce", c2: "#01cdfe", c3: "#b967ff", dim: 0.25,
  },
  Minimal: {
    align: "left", fill: "line", sung: "#ffffff", unsung: "rgba(255,255,255,0.25)", glow: 0, blur: 0, scale: 1,
    lift: 0, weight: 600, flicker: false, bg: "solid", c1: "#111114", dim: 0, uppercase: false, speed: 400,
  },
};

export const FONTS = [
  "Inter, system-ui, sans-serif",
  "'SF Pro Display', Inter, sans-serif",
  "Poppins, sans-serif",
  "Montserrat, sans-serif",
  "'Bebas Neue', Oswald, Impact, sans-serif",
  "'Playfair Display', Georgia, serif",
  "'JetBrains Mono', monospace",
  "'Comic Neue', 'Comic Sans MS', cursive",
];
