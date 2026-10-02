<script lang="ts">
  import { FONTS, PRESETS, type Settings } from "./settings";

  let { s, onclose }: { s: Settings; onclose: () => void } = $props();
  const pick = (name: string) => Object.assign(s, PRESETS[name], { preset: name });
</script>

<aside>
  <header>
    <h2>Estilo</h2>
    <button class="ghost" onclick={onclose} title="Cerrar (S)">✕</button>
  </header>

  <div class="presets">
    {#each Object.keys(PRESETS) as name (name)}
      <button class:on={s.preset === name} onclick={() => pick(name)}>{name}</button>
    {/each}
  </div>

  <div class="grid">
    <h3>Texto</h3>
    <label>Fuente <input list="fonts" bind:value={s.font} /></label>
    <datalist id="fonts">{#each FONTS as f (f)}<option value={f}></option>{/each}</datalist>
    <label>Tamaño <input type="range" min="18" max="110" bind:value={s.size} /><span>{s.size}px</span></label>
    <label>Grosor <input type="range" min="300" max="900" step="100" bind:value={s.weight} /><span>{s.weight}</span></label>
    <label>Interlineado <input type="range" min="0" max="1.5" step="0.05" bind:value={s.lineGap} /><span>{s.lineGap}em</span></label>
    <label>Alineación
      <select bind:value={s.align}><option value="left">Izquierda</option><option value="center">Centro</option></select>
    </label>
    <label class="check"><input type="checkbox" bind:checked={s.uppercase} /> MAYÚSCULAS</label>

    <h3>Color y efectos</h3>
    <label>Cantado <input type="color" bind:value={s.sung} /></label>
    <label>Por cantar <input bind:value={s.unsung} placeholder="rgba(255,255,255,.35)" /></label>
    <label>Resplandor <input type="color" bind:value={s.glowColor} /><input type="range" min="0" max="50" bind:value={s.glow} /><span>{s.glow}px</span></label>
    <label>Barrido
      <select bind:value={s.fill}>
        <option value="word">Karaoke por palabra</option><option value="line">Línea entera</option><option value="none">Sin barrido</option>
      </select>
    </label>
    <label>Zoom activa <input type="range" min="0.9" max="1.3" step="0.01" bind:value={s.scale} /><span>×{s.scale}</span></label>
    <label>Salto palabra <input type="range" min="0" max="12" bind:value={s.lift} /><span>{s.lift}px</span></label>
    <label class="check"><input type="checkbox" bind:checked={s.flicker} /> Parpadeo neón</label>

    <h3>Movimiento</h3>
    <label>Velocidad <input type="range" min="150" max="1500" step="50" bind:value={s.speed} /><span>{s.speed}ms</span></label>
    <label>Altura activa <input type="range" min="0.15" max="0.7" step="0.01" bind:value={s.anchor} /><span>{Math.round(s.anchor * 100)}%</span></label>

    <h3>Fondo</h3>
    <label>Tipo
      <select bind:value={s.bg}>
        <option value="aurora">Aurora</option><option value="cover">Carátula</option><option value="solid">Color</option><option value="transparent">Transparente</option>
      </select>
    </label>
    <label>Colores <input type="color" bind:value={s.c1} /><input type="color" bind:value={s.c2} /><input type="color" bind:value={s.c3} /></label>
    <label>Oscurecer <input type="range" min="0" max="0.9" step="0.05" bind:value={s.dim} /><span>{Math.round(s.dim * 100)}%</span></label>

    <h3>Sincronía</h3>
    <label>Desfase global <input type="range" min="-2" max="2" step="0.05" bind:value={s.offset} /><span>{s.offset > 0 ? "+" : ""}{s.offset}s</span></label>
  </div>
</aside>

<style>
  aside {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(380px, 100vw);
    overflow-y: auto;
    padding: 16px;
    box-sizing: border-box;
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
  h3 {
    margin: 14px 0 2px;
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    opacity: 0.6;
  }
  .presets {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 12px 0 4px;
  }
  .grid {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
  }
  label > :first-child:not(input) {
    flex: 1;
  }
  label input[type="range"] {
    flex: 1;
    min-width: 0;
  }
  label input:not([type]) {
    flex: 1;
    min-width: 0;
  }
  label span {
    width: 52px;
    text-align: right;
    font-variant-numeric: tabular-nums;
    opacity: 0.7;
  }
  .check {
    gap: 6px;
  }
  input[type="color"] {
    width: 34px;
    height: 24px;
    padding: 0;
    border: none;
    background: none;
  }
</style>
