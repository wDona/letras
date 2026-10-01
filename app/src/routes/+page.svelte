<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { open } from "@tauri-apps/plugin-dialog";
  import { onMount } from "svelte";
  import Background from "$lib/Background.svelte";
  import Editor from "$lib/Editor.svelte";
  import Lyrics from "$lib/Lyrics.svelte";
  import Settings from "$lib/Settings.svelte";
  import {
    SOURCE_LABEL, engine, initPaths, log, lyricsPath, readJson, settingsPath, writeJson,
    type EngineEvent, type Lyrics as Doc, type Track,
  } from "$lib/engine";
  import { DEFAULTS, type Settings as S } from "$lib/settings";

  type Player = Track & { player: string; playing: boolean; position: number };

  let s = $state<S>({ ...DEFAULTS });
  let ready = $state(false);
  let mode = $state<"mpris" | "file">("mpris");
  let track = $state<Track | null>(null);
  let player = "";
  let playing = ""; // reproductor que se está mostrando (al que van los saltos)
  let filePath = ""; // fichero original en modo fichero (la IA lo usa en vez de descargar)
  let key = $state("");
  let doc = $state<Doc | null>(null);
  let searching = $state(false);
  let job = $state<{ label: string; step?: number; total?: number; pct?: number } | null>(null);
  let error = $state("");
  let panel = $state<"" | "edit" | "style">("");
  let now = $state(0);
  let idle = $state(false);
  let audio: HTMLAudioElement;
  let paused = $state(true);
  let findId = "";

  // reloj: MPRIS da la posición cada medio segundo; entre medias se interpola
  let anchor = { pos: 0, at: 0, playing: false };
  const clock = () =>
    mode === "file" ? (audio?.currentTime ?? 0) : anchor.pos + (anchor.playing ? (performance.now() - anchor.at) / 1000 : 0);

  async function poll() {
    if (mode !== "mpris") return;
    const p = await invoke<Player | null>("player", { prefer: player || null }).catch(() => null);
    if (!p || !p.title) {
      track = null;
      return;
    }
    if (/firefox|chrom|brave|vivaldi|opera/i.test(p.player)) Object.assign(p, fromVideo(p.artist, p.title));
    if (p.playing !== anchor.playing || Math.abs(clock() - p.position) > 0.3)
      anchor = { pos: p.position, at: performance.now(), playing: p.playing };
    if (p.playing) player = p.player; // el último que sonó manda cuando todos están en pausa
    playing = p.player;
    if (!track || p.title !== track.title || p.artist !== track.artist) {
      track = { artist: p.artist, title: p.title, album: p.album, duration: p.duration, url: p.url, art: p.art };
      load(track);
    } else track.art = p.art;
  }

  /** Navegador: «Artista - Canción (Official Video)» del canal X -> artista y canción limpios. */
  function fromVideo(artist: string, title: string) {
    const clean = title
      .replace(/\s*[([][^)\]]*\b(official|oficial|lyrics?|letra|video|vídeo|audio|visuali[sz]er|hd|4k|mv|live|en vivo)\b[^)\]]*[)\]]/gi, "")
      .replace(/\s*\|.*$/, "")
      .trim();
    const m = clean.match(/^(.+?)\s+[-–—]\s+(.+)$/);
    return m ? { artist: m[1], title: m[2] } : { artist: artist.replace(/\s*-\s*Topic$/, ""), title: clean };
  }

  function progress(e: EngineEvent) {
    if (e.event === "progress") job = { label: e.label, step: e.step, total: e.total };
    else if (e.event === "pct" && job) job.pct = e.value;
    else if (e.event === "waiting") job = { label: e.label };
  }

  async function load(t: Track, force = false) {
    if (findId) void invoke("cancel", { id: findId });
    const id = (findId = crypto.randomUUID());
    doc = null;
    error = "";
    searching = true;
    try {
      const r = await engine<{ key: string; lyrics: Doc | null }>(
        ["find", t.artist, t.title, t.album, String(Math.round(t.duration)), force ? "force" : ""], () => {}, id,
      );
      if (id !== findId) return;
      key = r.key;
      doc = r.lyrics;
    } catch (e) {
      if (id === findId && e !== "cancelado") error = String(e);
    } finally {
      if (id === findId) searching = false;
    }
  }

  async function save(d: Doc) {
    doc = d;
    await writeJson(lyricsPath(key), d);
  }

  async function runAI(d?: Doc) {
    if (!track) return;
    if (d) await save(d);
    error = "";
    job = { label: "Preparando IA" };
    try {
      const audioSrc = mode === "file" ? filePath : (track.url ?? "");
      const r = await engine<{ lyrics: Doc }>(["ai", track.artist, track.title, audioSrc], progress);
      doc = r.lyrics;
    } catch (e) {
      error = `IA: ${e}`;
    } finally {
      job = null;
    }
  }

  async function openFile() {
    const path = await open({ filters: [{ name: "Audio", extensions: ["mp3", "flac", "ogg", "opus", "m4a", "wav", "aac", "webm", "mp4", "mkv"] }] });
    if (!path) return;
    job = { label: "Abriendo" };
    try {
      const r = await engine<Track & { key: string; wav: string }>(["open", path], progress);
      const bytes = await invoke<ArrayBuffer>("read_file", { path: r.wav });
      if (audio.src) URL.revokeObjectURL(audio.src);
      audio.src = URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
      mode = "file";
      filePath = path;
      track = { artist: r.artist, title: r.title, album: r.album, duration: r.duration };
      void audio.play();
      load(track);
    } catch (e) {
      error = String(e);
    } finally {
      job = null;
    }
  }

  function backToPlayer() {
    audio.pause();
    mode = "mpris";
    track = null;
    void poll();
  }

  function seek(t: number) {
    const to = Math.max(0, t + offset() - 0.05);
    if (mode === "file") audio.currentTime = to;
    else {
      void invoke("player_seek", { name: playing, secs: to });
      anchor = { pos: to, at: performance.now(), playing: anchor.playing };
    }
  }

  /** Positivo = la letra va más tarde. */
  const offset = () => s.offset + (doc?.offset ?? 0);
  function nudge(by: number) {
    if (!doc) return;
    void save({ ...doc, offset: Math.round(((doc.offset ?? 0) + by) * 100) / 100 });
  }

  function onkey(e: KeyboardEvent) {
    if ((e.target as HTMLElement).closest("input, textarea, select")) return;
    const k = e.key.toLowerCase();
    if (k === "e") panel = panel === "edit" ? "" : "edit";
    else if (k === "s") panel = panel === "style" ? "" : "style";
    else if (k === "f" || e.key === "F11") {
      e.preventDefault();
      const w = getCurrentWindow();
      void w.isFullscreen().then((f) => w.setFullscreen(!f));
    } else if (e.key === "[") nudge(-0.1);
    else if (e.key === "]") nudge(0.1);
    else if (e.code === "Space" && mode === "file") {
      e.preventDefault();
      if (audio.paused) void audio.play();
      else audio.pause();
    } else if (e.key === "Escape") panel = "";
  }

  let idleTimer: ReturnType<typeof setTimeout>;
  function wake() {
    idle = false;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => (idle = true), 2500);
  }

  onMount(() => {
    void invoke("cancel_all");
    let raf = 0;
    const frame = () => {
      now = clock() - offset();
      raf = requestAnimationFrame(frame);
    };
    const iv = setInterval(poll, 500);
    window.addEventListener("error", (e) => log(`${e.message} @ ${e.filename}:${e.lineno}`));
    (async () => {
      await initPaths();
      Object.assign(s, await readJson(settingsPath(), DEFAULTS));
      ready = true;
      void poll();
      frame();
    })();
    wake();
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(iv);
    };
  });

  // ajustes al disco (y de ahí, en el futuro, a quickshell), con un respiro para no escribir en cada arrastre
  let saveTimer: ReturnType<typeof setTimeout>;
  $effect(() => {
    const snap = $state.snapshot(s);
    if (!ready) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => writeJson(settingsPath(), snap), 400);
  });

  const unsynced = $derived(doc && !doc.instrumental && !doc.lines.some((l) => l.t != null));
  const emptyDoc = (): Doc => ({ key, artist: track!.artist, title: track!.title, source: "manual", synced: false, instrumental: false, lines: [] });
</script>

<svelte:window onkeydown={onkey} onmousemove={wake} />

<Background {s} art={track?.art} />

<main class:idle class:shifted={panel !== ""}>
  {#if doc?.lines.length}
    <Lyrics lines={doc.lines} {now} {s} onseek={seek} />
  {:else}
    <div class="center">
      {#if !track}
        <h1>Nada sonando</h1>
        <p>Pon algo en Spotify, Firefox, mpv… o abre un fichero.</p>
        <button class="primary" onclick={openFile}>Abrir fichero</button>
      {:else if searching}
        <div class="spinner"></div>
        <p>Buscando letra de <b>{track.title}</b>…</p>
      {:else if doc?.instrumental}
        <h1>♪ Instrumental ♪</h1>
      {:else}
        <h1>Sin letra en internet</h1>
        <p>Ni sincronizada ni plana en ninguna fuente.</p>
        <div class="row">
          <button class="primary" disabled={!!job} onclick={() => runAI()}>Sacarla con IA</button>
          <button onclick={() => { doc = emptyDoc(); panel = "edit"; }}>Escribirla a mano</button>
          <button onclick={() => track && load(track, true)}>Buscar otra vez</button>
        </div>
      {/if}
    </div>
  {/if}
</main>

<nav class:idle={idle && panel === ""}>
  <div class="meta">
    {#if track}
      <b>{track.title}</b><span>{track.artist}</span>
      {#if doc}
        <em class="badge" title="De dónde salió la letra">
          {SOURCE_LABEL[doc.source] ?? doc.source}{doc.synced_by === "ia" ? " · tiempos IA" : ""}{doc.edited ? " · editada" : ""}
        </em>
      {/if}
    {/if}
  </div>
  <div class="row">
    {#if doc}
      <span class="nudge" title="Desfase de esta canción ([ y ])">
        <button class="ghost" onclick={() => nudge(-0.1)}>−</button>
        {(doc.offset ?? 0) > 0 ? "+" : ""}{(doc.offset ?? 0).toFixed(1)}s
        <button class="ghost" onclick={() => nudge(0.1)}>+</button>
      </span>
    {/if}
    {#if mode === "file"}
      <button onclick={backToPlayer}>Reproductor del sistema</button>
    {:else}
      <button onclick={openFile}>Abrir fichero</button>
    {/if}
    {#if track}
      <button onclick={() => track && load(track, true)} title="Ignora la caché">Buscar otra vez</button>
      <button class:on={panel === "edit"} onclick={() => { if (!doc) doc = emptyDoc(); panel = panel === "edit" ? "" : "edit"; }}>Editar (E)</button>
    {/if}
    <button class:on={panel === "style"} onclick={() => (panel = panel === "style" ? "" : "style")}>Estilo (S)</button>
  </div>
</nav>

{#if unsynced && !job}
  <div class="banner">
    Letra sin tiempos ({SOURCE_LABEL[doc!.source] ?? doc!.source}).
    <button class="primary" onclick={() => runAI()}>Sincronizar con IA</button>
    <button onclick={() => (panel = "edit")}>A mano</button>
  </div>
{/if}

{#if job}
  <div class="banner">
    <div class="spinner small"></div>
    {job.label}{job.total ? ` (${job.step}/${job.total})` : ""}{job.pct != null ? ` · ${job.pct}%` : ""}
  </div>
{/if}

{#if error}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="banner err" onclick={() => (error = "")}>{error}</div>
{/if}

<div class="transport" class:hidden={mode !== "file"} class:idle={idle && panel === ""}>
  <button class="ghost" onclick={() => (audio.paused ? audio.play() : audio.pause())}>{paused ? "▶" : "⏸"}</button>
  <input type="range" min="0" max={track?.duration || 1} step="0.1" value={now + offset()} oninput={(e) => (audio.currentTime = +e.currentTarget.value)} />
  <audio bind:this={audio} bind:paused></audio>
</div>

{#if panel === "edit" && doc}
  {#key doc.key + (doc.synced_by ?? "")}
    <Editor {doc} {now} busy={!!job} onsave={save} onai={runAI} onseek={seek} onclose={() => (panel = "")} />
  {/key}
{:else if panel === "style"}
  <Settings {s} onclose={() => (panel = "")} />
{/if}

<style>
  :global(html, body) {
    margin: 0;
    height: 100%;
    background: transparent;
    color: #fff;
    font-family: Inter, system-ui, sans-serif;
    overflow: hidden;
    user-select: none;
  }
  :global(button) {
    font: inherit;
    font-size: 13px;
    color: #fff;
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 6px 12px;
    cursor: pointer;
    transition: background 0.15s, transform 0.1s;
  }
  :global(button:hover:not(:disabled)) {
    background: rgba(255, 255, 255, 0.2);
  }
  :global(button:active:not(:disabled)) {
    transform: scale(0.96);
  }
  :global(button:disabled) {
    opacity: 0.4;
    cursor: default;
  }
  :global(button.primary) {
    background: linear-gradient(135deg, #ff3d7f, #7a3dff);
    border-color: transparent;
  }
  :global(button.on) {
    background: rgba(255, 255, 255, 0.85);
    color: #111;
  }
  :global(button.ghost) {
    background: none;
    border-color: transparent;
    padding: 4px 8px;
  }
  :global(input:not([type="range"]):not([type="color"]):not([type="checkbox"]), select, textarea) {
    font: inherit;
    font-size: 13px;
    color: #fff;
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    padding: 5px 8px;
    user-select: text;
  }
  :global(select option) {
    background: #1a1726;
  }
  :global(input[type="range"]) {
    accent-color: #ff3d7f;
  }

  main {
    position: fixed;
    inset: 0;
    transition: right 0.35s cubic-bezier(0.2, 0.9, 0.3, 1);
  }
  main.shifted {
    right: min(440px, 45vw);
  }
  main.idle {
    cursor: none;
  }
  .center {
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    text-align: center;
    padding: 16px;
  }
  .center h1 {
    margin: 0;
    font-size: 40px;
    font-weight: 800;
  }
  .center p {
    margin: 0;
    opacity: 0.75;
  }
  .row {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
  }
  nav {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px 16px;
    background: linear-gradient(rgba(0, 0, 0, 0.45), transparent);
    transition: opacity 0.6s, transform 0.6s;
    z-index: 5;
  }
  nav.idle,
  .transport.idle {
    opacity: 0;
    pointer-events: none;
  }
  nav.idle {
    transform: translateY(-8px);
  }
  .meta {
    display: flex;
    gap: 10px;
    align-items: baseline;
    min-width: 0;
  }
  .meta span {
    opacity: 0.7;
  }
  .badge {
    font-style: normal;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.12);
  }
  .nudge {
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    opacity: 0.85;
  }
  .banner {
    position: fixed;
    left: 50%;
    bottom: 64px;
    transform: translateX(-50%);
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 10px 16px;
    border-radius: 14px;
    font-size: 14px;
    background: rgba(14, 12, 22, 0.75);
    backdrop-filter: blur(20px);
    border: 1px solid rgba(255, 255, 255, 0.1);
    z-index: 6;
    max-width: calc(100vw - 32px);
    box-sizing: border-box;
  }
  .banner.err {
    bottom: 120px;
    background: rgba(120, 10, 30, 0.8);
    cursor: pointer;
    user-select: text;
  }
  .transport {
    position: fixed;
    left: 16px;
    right: 16px;
    bottom: 12px;
    display: flex;
    gap: 8px;
    align-items: center;
    transition: opacity 0.6s;
    z-index: 5;
  }
  .transport.hidden {
    display: none;
  }
  .transport input {
    flex: 1;
  }
  .spinner {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    border: 3px solid rgba(255, 255, 255, 0.15);
    border-top-color: #fff;
    animation: spin 0.8s linear infinite;
  }
  .spinner.small {
    width: 14px;
    height: 14px;
    border-width: 2px;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
