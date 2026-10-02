<script lang="ts">
  import type { Line, Word } from "./engine";
  import type { Settings } from "./settings";

  let { lines, now: playing, s, onseek }: { lines: Line[]; now: number; s: Settings; onseek: (t: number) => void } = $props();
  // adelanto: la palabra ya se ve iluminada cuando empieza a sonar (el barrido tarda un poco en notarse)
  const LEAD = 0.1;
  const now = $derived(playing + LEAD);

  let box: HTMLDivElement;
  let list: HTMLDivElement;
  let y = $state(0);

  const synced = $derived(lines.some((l) => l.t != null));
  /** Última línea que ya empezó (-1 en la intro). */
  const active = $derived.by(() => {
    let i = -1;
    for (let k = 0; k < lines.length; k++) if (lines[k].t != null && lines[k].t! <= now) i = k;
    return i;
  });
  const startOf = (k: number) => lines[k]?.t ?? null;
  const endOf = (k: number) => lines[k].end ?? lines.slice(k + 1).find((l) => l.t != null)?.t ?? lines[k].t! + 5;

  /** Pausa larga (intro o instrumental): puntos que se llenan hasta la siguiente línea. */
  const gap = $derived.by(() => {
    if (!synced) return null;
    const from = active < 0 ? 0 : endOf(active);
    const next = lines.slice(active + 1).find((l) => l.t != null && l.text)?.t;
    if (next == null || next - from < 4 || now < from + 0.3) return null;
    return Math.min(1, (now - from) / (next - from));
  });

  /** Palabras con inicio y fin. Sin tiempos por palabra, se reparten por letras dentro de la línea. */
  // japonés/chino: sin espacio entre dos caracteres (el motor los separa uno a uno para sincronizarlos)
  const CJK = /[\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/;
  const spaceAfter = (w: string, next?: string) => (next && CJK.test(w.at(-1)!) && CJK.test(next[0]) ? "" : " ");

  function wordsOf(line: Line, k: number): Word[] {
    const end = line.t == null ? 0 : endOf(k);
    if (line.words?.length) {
      // el barrido sigue hasta que empieza la siguiente: sin parones en los huecos cortos entre palabras
      return line.words.map((w, i) => {
        const next = line.words![i + 1]?.t ?? end;
        return { ...w, end: w.end == null || next - w.end < 1 ? next : w.end };
      });
    }
    const parts = line.text.split(/\s+/).filter(Boolean);
    const total = parts.reduce((n, p) => n + p.length + 1, 0);
    // cantar ocupa ~85 % de la línea; el resto es respiración antes de la siguiente
    const dur = Math.min((end - (line.t ?? 0)) * 0.85, total * 0.09 + 0.6);
    let acc = 0;
    return parts.map((w) => {
      const t = (line.t ?? 0) + (acc / total) * dur;
      acc += w.length + 1;
      return { t, end: (line.t ?? 0) + (acc / total) * dur, w };
    });
  }
  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  const prog = (w: Word) => clamp((now - w.t) / Math.max(0.05, (w.end ?? w.t) - w.t));

  // la línea activa se coloca a la altura `anchor`; offsetTop no ve los transform, así que no hay bucle
  $effect(() => {
    void s.size, s.lineGap, s.font, lines;
    const el = list?.children[Math.max(active, 0)] as HTMLElement | undefined;
    if (el && box) y = el.offsetTop + el.offsetHeight / 2 - box.clientHeight * s.anchor;
  });
</script>

<div
  class="box"
  class:plain={!synced}
  bind:this={box}
  style:--sung={s.sung}
  style:--unsung={s.unsung}
  style:--glow={s.glowColor}
  style:--lift="{s.lift}px"
  style:font-family={s.font}
  style:font-size="{s.size}px"
  style:font-weight={s.weight}
  style:text-align={s.align}
  style:text-transform={s.uppercase ? "uppercase" : "none"}
>
  <div class="list" bind:this={list}>
    {#each lines as line, k (k)}
      {@const d = k - active}
      {@const on = synced && k === active && gap == null}
      {@const past = synced && (d < 0 || (d === 0 && gap != null))}
      {@const far = Math.min(Math.abs(d), 6)}
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
      <div
        class="line"
        class:on
        class:past
        class:flicker={on && s.flicker}
        class:blank={!line.text}
        style:margin-bottom="{s.lineGap}em"
        style:transform={synced ? `translateY(${-y}px) scale(${on ? s.scale : 0.92 + 0.08 * (1 - far / 6)})` : null}
        style:transform-origin={s.align === "center" ? "center" : "left center"}
        style:transition="transform {s.speed}ms cubic-bezier(.2,.85,.25,1.08) {Math.max(0, Math.min(d, 8)) * 45}ms, opacity 400ms"
        style:text-shadow={on && s.glow ? `0 0 ${s.glow}px var(--glow)` : null}
        style:opacity={synced ? (on ? 1 : Math.abs(d) > 8 ? 0 : 1 - far * 0.12) : 1}
        onclick={() => line.t != null && onseek(line.t)}
      >
        {#if !line.text}
          <span class="note">♪</span>
        {:else if !synced || s.fill === "none"}
          {line.text}
        {:else if s.fill === "line"}
          <span class="w" style:--p={past || on ? 1 : 0}>{line.text}</span>
        {:else}
          {#each wordsOf(line, k) as w, i (i)}
            {@const p = past ? 1 : on ? prog(w) : 0}
            <span class="w" class:sing={on && p > 0 && p < 1} style:--p={p}>{w.w}</span>{spaceAfter(w.w, line.words?.[i + 1]?.w)}
          {/each}
        {/if}
      </div>
    {/each}
  </div>

  {#if gap != null}
    <div class="dots" style:top="{s.anchor * 100}%" style:justify-content={s.align === "center" ? "center" : "flex-start"}>
      {#each [0, 1, 2] as i (i)}
        {@const p = clamp(gap * 3 - i)}
        <span style:opacity={0.25 + 0.75 * p} style:transform="scale({0.6 + 0.5 * p})"></span>
      {/each}
    </div>
  {/if}
</div>

<style>
  .box {
    position: absolute;
    inset: 0;
    overflow: hidden;
    padding: 0 8vw;
    line-height: 1.18;
    letter-spacing: -0.01em;
  }
  .box.plain {
    overflow-y: auto;
    padding-top: 15vh;
    padding-bottom: 30vh;
    font-size: 0.7em;
  }
  .list {
    position: relative;
    padding-bottom: 80vh;
  }
  .line {
    color: var(--unsung);
    cursor: pointer;
    text-wrap: balance;
  }
  .plain .line {
    color: var(--sung);
    opacity: 0.85;
    cursor: default;
  }
  .line.blank {
    font-size: 0.6em;
    opacity: 0.4;
  }
  .line.past {
    color: var(--unsung);
  }
  .w {
    display: inline-block;
    color: transparent;
    background: linear-gradient(
      90deg,
      var(--sung) calc(var(--p) * 115% - 15%),
      var(--unsung) calc(var(--p) * 115%)
    );
    -webkit-background-clip: text;
    background-clip: text;
    transition: transform 0.3s cubic-bezier(0.3, 1.6, 0.5, 1);
    padding-bottom: 0.08em; /* que el recorte del fondo no se coma los descendentes */
  }
  .w.sing {
    transform: translateY(calc(var(--lift) * -1));
  }
  .flicker {
    animation: flicker 3.2s infinite;
  }
  @keyframes flicker {
    0%, 18%, 22%, 25%, 53%, 57%, 100% { opacity: 1; }
    20%, 24%, 55% { opacity: 0.55; }
  }
  .note {
    display: inline-block;
    animation: bob 1.6s ease-in-out infinite;
  }
  @keyframes bob {
    50% { transform: translateY(-0.15em) rotate(-8deg); }
  }
  .dots {
    position: absolute;
    left: 8vw;
    right: 8vw;
    display: flex;
    gap: 0.35em;
    transform: translateY(-50%);
    font-size: inherit;
  }
  .dots span {
    width: 0.32em;
    height: 0.32em;
    border-radius: 50%;
    background: var(--sung);
    box-shadow: 0 0 0.4em var(--glow);
    transition: transform 0.25s, opacity 0.25s;
    animation: breathe 2.4s ease-in-out infinite;
  }
  @keyframes breathe {
    50% { filter: brightness(1.4); }
  }
</style>
