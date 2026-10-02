<script lang="ts">
  import { doubtful, fmtTime, parseTime, type Line, type Lyrics } from "./engine";

  let {
    doc,
    now,
    busy,
    onsave,
    onai,
    onseek,
    onclose,
  }: {
    doc: Lyrics;
    now: number;
    busy: boolean;
    onsave: (d: Lyrics) => void;
    onai: (d: Lyrics) => void;
    onseek: (t: number) => void;
    onclose: () => void;
  } = $props();

  // copia propia: nada se toca hasta Guardar (el padre recrea el editor con {#key} si cambia doc)
  // svelte-ignore state_referenced_locally
  let lines = $state<Line[]>(structuredClone($state.snapshot(doc.lines)));
  let dirty = $state(false);
  let tap = $state(-1); // línea que marcará el próximo Espacio; -1 = modo tap apagado
  let paste = $state<string | null>(null);
  let rows: HTMLDivElement;

  const doubts = $derived(lines.filter((l) => doubtful(l).length).length);
  const touch = () => (dirty = true);
  function setText(l: Line, text: string) {
    l.text = text;
    delete l.words; // los tiempos por palabra ya no valen
    touch();
  }
  function setT(l: Line, t: number | null) {
    l.t = t;
    delete l.end;
    delete l.words;
    touch();
  }
  function insert(k: number) {
    lines.splice(k + 1, 0, { t: null, text: "" });
    touch();
  }
  function remove(k: number) {
    lines.splice(k, 1);
    touch();
  }
  function shift(by: number) {
    for (const l of lines) {
      if (l.t != null) l.t = Math.max(0, Math.round((l.t + by) * 1000) / 1000);
      if (l.end != null) l.end = Math.max(0, l.end + by);
      for (const w of l.words ?? []) {
        w.t = Math.max(0, w.t + by);
        if (w.end != null) w.end = Math.max(0, w.end + by);
      }
    }
    touch();
  }
  /** Texto pegado: una línea por renglón. Si el número de líneas no cambia, conserva los tiempos. */
  function applyPaste() {
    const texts = paste!.split("\n").map((t) => t.trim());
    const keep = texts.length === lines.length;
    lines = texts.map((text, k) => (keep && lines[k].text === text ? lines[k] : { t: keep ? lines[k].t : null, text }));
    paste = null;
    touch();
  }
  function result(): Lyrics {
    const out = $state.snapshot(lines) as Line[];
    const synced = out.some((l) => l.t != null);
    if (out.every((l) => l.t != null)) out.sort((a, b) => a.t! - b.t!);
    return { ...$state.snapshot(doc), lines: out, synced, edited: true, source: doc.source || "manual" };
  }
  function save() {
    onsave(result());
    dirty = false;
  }

  function onkey(e: KeyboardEvent) {
    if (tap < 0 || (e.target as HTMLElement).closest("input, textarea")) return;
    if (e.code === "Space") {
      e.preventDefault();
      e.stopPropagation();
      setT(lines[tap], Math.round(now * 100) / 100);
      tap = Math.min(tap + 1, lines.length - 1);
    } else if (e.code === "Backspace") {
      e.preventDefault();
      tap = Math.max(0, tap - 1);
    }
  }
  $effect(() => {
    if (tap >= 0) (rows?.children[tap] as HTMLElement)?.scrollIntoView({ block: "center", behavior: "smooth" });
  });
</script>

<svelte:window onkeydowncapture={onkey} />

<aside>
  <header>
    <h2>Editar letra</h2>
    <button class="ghost" onclick={onclose} title="Cerrar (E)">✕</button>
  </header>

  <div class="tools">
    <button class="primary" disabled={!dirty} onclick={save}>Guardar</button>
    <button onclick={() => (paste = lines.map((l) => l.text).join("\n"))}>Pegar / editar texto</button>
    <button class:on={tap >= 0} onclick={() => (tap = tap >= 0 ? -1 : Math.max(0, lines.findIndex((l) => l.t == null)))}>
      {tap >= 0 ? "Parar tap" : "Sincronizar a mano"}
    </button>
    <button disabled={busy || !lines.some((l) => l.text)} onclick={() => { const d = result(); dirty = false; onai(d); }}
      title="Mantiene tu texto y le pone tiempos con Whisper. Tras la primera vez es instantáneo.">
      Sincronizar con IA
    </button>
    <span class="nudge">
      Todo
      <button onclick={() => shift(-0.1)}>−0.1 s</button>
      <button onclick={() => shift(0.1)}>+0.1 s</button>
    </span>
  </div>

  {#if doc.source === "ia" && !doc.edited}
    <p class="hint warn">
      Transcrita por IA: revísala.{doubts ? ` ${doubts} líneas con palabras dudosas marcadas en amarillo (▶ para oírlas).` : ""}
    </p>
  {/if}

  {#if tap >= 0}
    <p class="hint">Reproduce la canción y pulsa <kbd>Espacio</kbd> justo cuando empiece cada línea resaltada. <kbd>⌫</kbd> vuelve una atrás.</p>
  {/if}

  {#if paste != null}
    <div class="paste">
      <textarea bind:value={paste} rows="16" placeholder="Una línea de la letra por renglón. Renglón vacío = pausa."></textarea>
      <div class="tools">
        <button class="primary" onclick={applyPaste}>Aplicar</button>
        <button onclick={() => (paste = null)}>Cancelar</button>
      </div>
    </div>
  {/if}

  <div class="rows" bind:this={rows}>
    {#each lines as l, k (k)}
      <div class="row" class:doubt={doubtful(l).length} title={doubtful(l).length ? `La IA no tiene claro: ${doubtful(l).join(", ")}` : undefined} class:tapnext={k === tap} class:playing={l.t != null && l.t <= now && (lines[k + 1]?.t ?? Infinity) > now}>
        <input class="time" value={fmtTime(l.t)} placeholder="--:--" onchange={(e) => setT(l, parseTime(e.currentTarget.value))} />
        <button class="ghost" title="Poner el tiempo actual" onclick={() => setT(l, Math.round(now * 100) / 100)}>⏱</button>
        <button class="ghost" title="Ir aquí" disabled={l.t == null} onclick={() => onseek(l.t!)}>▶</button>
        <input class="text" value={l.text} oninput={(e) => setText(l, e.currentTarget.value)} />
        <button class="ghost" title="Línea debajo" onclick={() => insert(k)}>＋</button>
        <button class="ghost" title="Borrar" onclick={() => remove(k)}>✕</button>
      </div>
    {:else}
      <p class="hint">Sin letra. Pega el texto o añade líneas.</p>
      <button onclick={() => insert(-1)}>＋ Añadir línea</button>
    {/each}
  </div>
</aside>

<style>
  aside {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(560px, 100vw);
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 16px;
    background: rgb(14, 12, 22);
    border-left: 1px solid rgba(255, 255, 255, 0.08);
    z-index: 10;
    animation: slide 0.35s cubic-bezier(0.2, 0.9, 0.3, 1);
  }
  @keyframes slide {
    from { transform: translateX(40px); opacity: 0; }
  }
  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  h2 {
    margin: 0;
    font-size: 18px;
  }
  .tools {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .nudge {
    margin-left: auto;
    font-size: 12px;
    opacity: 0.8;
  }
  .rows {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .row {
    display: flex;
    gap: 4px;
    align-items: center;
    padding: 2px 4px;
    border-radius: 8px;
  }
  .row.playing {
    background: rgba(255, 255, 255, 0.07);
  }
  .row.doubt {
    box-shadow: inset 3px 0 #ffc23d;
  }
  .row.doubt .text {
    border-color: rgba(255, 194, 61, 0.5);
  }
  .hint.warn {
    color: #ffc23d;
    opacity: 1;
  }
  .row.tapnext {
    background: rgba(255, 61, 127, 0.25);
    outline: 1px solid #ff3d7f;
  }
  .time {
    width: 70px;
    font-variant-numeric: tabular-nums;
  }
  .text {
    flex: 1;
  }
  .paste textarea {
    width: 100%;
    box-sizing: border-box;
    resize: vertical;
  }
  .hint {
    font-size: 13px;
    opacity: 0.75;
    margin: 0;
  }
  kbd {
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(255, 255, 255, 0.15);
  }
</style>
