<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";
  import Editor from "$lib/Editor.svelte";
  import {
    SOURCE_LABEL, SYNCED_SOURCES, dataPath, engine, fmtTime, initPaths, log, lyricsPath, readText, writeJson, writeText,
    type EngineEvent, type Lyrics as Doc, type Song, type Track,
  } from "$lib/engine";

  type Player = Track & { player: string; playing: boolean; position: number };
  type Job = { key: string; artist: string; title: string; duration: number; mode: "" | "lineas" };

  let songs = $state<Song[]>([]);
  let orphanFailed = $state<string[]>([]); // fallidas del prefetch sin letra guardada
  let track = $state<Track | null>(null); // lo que suena en Spotify
  let trackKey = $state("");
  let searching = $state(false);
  let queue = $state<Job[]>([]);
  let job = $state<{ key: string; title: string; label: string; step?: number; total?: number; pct?: number } | null>(null);
  let error = $state("");
  let now = $state(0);

  // vista
  let q = $state("");
  let filter = $state("all");
  let sort = $state<"recent" | "artist" | "title" | "doubts" | "size">("recent");
  let picked = $state<string[]>([]); // selección para acciones en bloque
  let selected = $state(""); // canción del panel de detalle
  let editing = $state<Doc | null>(null);
  let panel = $state<"" | "add" | "playlists">("");
  let confirmDel = $state("");
  let searchBox: HTMLInputElement;

  // ---------------------------------------------------------------- estado de cada canción

  type Sync = "origin" | "manual" | "ia" | "ia-fast" | "none" | "instrumental" | "empty";
  const timesFrom = (s: Song) => s.times_from ?? s.source;
  /** «Genius + LRCLIB»: texto de una, tiempos por línea de la otra. */
  const textLabel = (s: Song) => (SOURCE_LABEL[s.source] ?? s.source).replace(" (sin tiempos)", "") + (s.times_from ? ` + ${SOURCE_LABEL[s.times_from] ?? s.times_from}` : "");
  function syncOf(s: Song): Sync {
    if (s.instrumental) return "instrumental";
    if (!s.lines) return "empty";
    if (s.synced_by === "ia") return s.ai_mode === "lineas" ? "ia-fast" : "ia";
    if (s.timed) return s.source === "manual" || (s.edited && !SYNCED_SOURCES.has(timesFrom(s))) ? "manual" : "origin";
    return "none";
  }
  const SYNC_LABEL: Record<Sync, string> = {
    origin: "Venía sincronizada",
    manual: "Sincronizada a mano",
    ia: "Sincronizada con IA",
    "ia-fast": "IA rápida",
    none: "Sin tiempos",
    instrumental: "Instrumental",
    empty: "Sin letra",
  };
  function syncHint(s: Song) {
    const k = syncOf(s);
    const prior = SYNCED_SOURCES.has(timesFrom(s)) ? ` sobre los tiempos que ya traía ${SOURCE_LABEL[timesFrom(s)]}` : "";
    const text = s.times_from ? ` (texto de ${SOURCE_LABEL[s.source] ?? s.source}, mejor que el suyo)` : "";
    if (k === "ia") return `Whisper + alineado forzado${prior || (s.source === "ia" ? " (letra transcrita por la IA)" : " sobre texto sin tiempos")}`;
    if (k === "ia-fast") return `Sin Whisper: palabra a palabra${prior}${text}`;
    if (k === "origin") return `Tiempos por línea de ${SOURCE_LABEL[timesFrom(s)] ?? timesFrom(s)}, sin IA${text}`;
    return "";
  }

  const FILTERS: [string, string, (s: Song) => boolean][] = [
    ["all", "Todas", () => true],
    ["origin", "Venían sincronizadas", (s) => syncOf(s) === "origin"],
    ["ia", "Sincronizadas con IA", (s) => syncOf(s).startsWith("ia")],
    ["transcribed", "Transcritas por IA", (s) => s.source === "ia"],
    ["manual", "A mano / editadas", (s) => syncOf(s) === "manual" || !!s.edited],
    ["review", "Por revisar", (s) => s.doubts > 0 && !s.edited],
    ["pending", "Sin sincronizar", (s) => ["none", "empty"].includes(syncOf(s))],
    ["hidden", "Ocultas", (s) => !!s.hidden],
    ["failed", "Fallidas", (s) => s.failed],
  ];
  const count = (f: (s: Song) => boolean) => songs.filter(f).length;

  const shown = $derived.by(() => {
    const test = FILTERS.find((f) => f[0] === filter)![2];
    const needle = q.trim().toLowerCase();
    const list = songs.filter((s) => test(s) && (!needle || `${s.artist} ${s.title} ${s.note ?? ""}`.toLowerCase().includes(needle)));
    const by: Record<typeof sort, (a: Song, b: Song) => number> = {
      recent: (a, b) => b.modified - a.modified,
      artist: (a, b) => a.artist.localeCompare(b.artist) || a.title.localeCompare(b.title),
      title: (a, b) => a.title.localeCompare(b.title),
      doubts: (a, b) => b.doubts - a.doubts,
      size: (a, b) => b.bytes - a.bytes,
    };
    return list.sort(by[sort]);
  });
  const current = $derived(songs.find((s) => s.key === selected) ?? null);
  const totalBytes = $derived(songs.reduce((n, s) => n + s.bytes, 0));
  const queued = (key: string) => job?.key === key || queue.some((j) => j.key === key);

  const fmtBytes = (b: number) => (b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${Math.round(b / 1e6)} MB` : b ? `${Math.round(b / 1e3)} kB` : "—");
  const fmtDate = (s: number) => new Date(s * 1000).toLocaleDateString("es", { day: "numeric", month: "short", year: "2-digit" });

  // ---------------------------------------------------------------- datos

  async function refresh() {
    try {
      const r = await engine<{ songs: Song[]; failed: string[] }>(["library"]);
      songs = r.songs;
      orphanFailed = r.failed;
      picked = picked.filter((k) => songs.some((s) => s.key === k));
    } catch (e) {
      error = String(e);
    }
  }

  async function loadDoc(key: string): Promise<Doc | null> {
    try {
      return JSON.parse(await readText(lyricsPath(key)));
    } catch {
      return null;
    }
  }

  /** Cambia campos del documento en disco (desfase, nota, oculta…) sin tocar la letra. */
  async function patch(key: string, change: Partial<Doc>) {
    const d = await loadDoc(key);
    if (!d) return;
    await writeJson(lyricsPath(key), { ...d, ...change });
    await refresh();
  }

  /** Guardar desde el editor: la letra es la suya, lo demás (desfase, nota…) lo que haya en disco. */
  async function saveDoc(d: Doc) {
    const disk = await loadDoc(d.key);
    const out = { ...d, offset: disk?.offset, word_offset: disk?.word_offset, note: disk?.note, hidden: disk?.hidden };
    await writeJson(lyricsPath(d.key), out);
    if (editing?.key === d.key) editing = out;
    await refresh();
  }

  // ---------------------------------------------------------------- IA: una a la vez (GPU)

  const aiFailed = new Set<string>(); // no reintentar sola en bucle lo que ya petó
  function progress(e: EngineEvent) {
    if (!job) return;
    if (e.event === "progress") Object.assign(job, { label: e.label, step: e.step, total: e.total, pct: undefined });
    else if (e.event === "pct") job.pct = e.value;
    else if (e.event === "waiting") Object.assign(job, { label: e.label, step: undefined, total: undefined });
  }

  function enqueue(j: Job) {
    if (!queued(j.key)) queue.push(j);
    void pump();
  }
  const jobFor = (s: Song, mode: Job["mode"] = ""): Job => ({ key: s.key, artist: s.artist, title: s.title, duration: s.duration ?? 0, mode });

  async function pump() {
    if (job || !queue.length) return;
    const j = queue.shift()!;
    job = { key: j.key, title: j.title, label: "Preparando IA" };
    try {
      const r = await engine<{ lyrics: Doc }>(["ai", j.artist, j.title, "", String(Math.round(j.duration)), j.mode], progress);
      aiFailed.delete(j.key);
      if (editing?.key === j.key) editing = r.lyrics;
    } catch (e) {
      aiFailed.add(j.key);
      error = `IA «${j.title}»: ${e}`;
    } finally {
      job = null;
    }
    await refresh();
    autoSync();
    void pump();
  }

  /** La que suena acaba sincronizada sola: sin tiempos -> la IA se los pone; sin letra -> la transcribe.
   *  Una vez: si falla, quedan los botones. */
  function autoSync() {
    if (!track || !trackKey || searching || aiFailed.has(trackKey)) return;
    const s = songs.find((x) => x.key === trackKey);
    if (s && (s.instrumental || s.timed)) return;
    enqueue({ key: trackKey, artist: track.artist, title: track.title, duration: track.duration, mode: "" });
  }

  // ---------------------------------------------------------------- Spotify

  let anchor = $state({ pos: 0, at: 0, playing: false });
  const clock = () => anchor.pos + (anchor.playing ? (performance.now() - anchor.at) / 1000 : 0);
  let findId = "";

  async function poll() {
    const p = await invoke<Player | null>("player", { prefer: null }).catch(() => null);
    if (!p?.title) {
      track = null;
      return;
    }
    if (p.playing !== anchor.playing || Math.abs(clock() - p.position) > 0.3) anchor = { pos: p.position, at: performance.now(), playing: p.playing };
    if (!track || p.title !== track.title || p.artist !== track.artist) {
      track = { artist: p.artist, title: p.title, album: p.album, duration: p.duration };
      trackKey = "";
      void find(track);
    }
  }

  /** Busca la letra (o la coge de la caché) de una canción; `force` vuelve a internet aunque ya esté. */
  async function find(t: { artist: string; title: string; album?: string; duration?: number }, force = false) {
    const isTrack = t === track;
    if (isTrack && findId) void invoke("cancel", { id: findId });
    const id = crypto.randomUUID();
    if (isTrack) {
      findId = id;
      searching = true;
    }
    try {
      const r = await engine<{ key: string }>(["find", t.artist, t.title, t.album ?? "", String(Math.round(t.duration ?? 0)), force ? "force" : ""], () => {}, id);
      if (isTrack && id === findId) trackKey = r.key;
      if (force) aiFailed.delete(r.key);
      return r.key;
    } catch (e) {
      if (e !== "cancelado") error = String(e);
    } finally {
      if (isTrack && id === findId) searching = false;
      await refresh();
      autoSync();
    }
  }

  function seek(t: number) {
    const to = Math.max(0, t + (editing?.offset ?? 0) - 0.05);
    void invoke("player_seek", { name: "spotify", secs: to });
    anchor = { pos: to, at: performance.now(), playing: anchor.playing };
  }

  // ---------------------------------------------------------------- acciones

  async function edit(key: string) {
    editing = await loadDoc(key);
    if (!editing) error = "no se pudo leer la letra";
  }

  async function restore(s: Song) {
    try {
      const r = await engine<{ lyrics: Doc }>(["restore", s.artist, s.title]);
      if (editing?.key === s.key) editing = r.lyrics;
    } catch (e) {
      error = String(e);
    }
    await refresh();
  }

  async function remove(key: string, what: "" | "audio") {
    confirmDel = "";
    try {
      await engine(["delete", key, what]);
    } catch (e) {
      error = String(e);
    }
    if (!what && selected === key) selected = "";
    await refresh();
  }

  // ---------------------------------------------------------------- selección y acciones en bloque

  let lastPick = ""; // ancla del shift-clic
  /** Clic en la casilla (o ctrl/shift-clic en la fila): alterna una, o con shift todo el tramo desde la anterior. */
  function pick(key: string, e: MouseEvent) {
    const keys = shown.map((s) => s.key);
    const a = keys.indexOf(lastPick), b = keys.indexOf(key);
    const range = e.shiftKey && a >= 0 ? keys.slice(Math.min(a, b), Math.max(a, b) + 1) : [key];
    picked = picked.includes(key) ? picked.filter((k) => !range.includes(k)) : [...new Set([...picked, ...range])];
    lastPick = key;
  }
  const pickedSongs = $derived(songs.filter((s) => picked.includes(s.key)));
  let bulkBusy = $state("");
  let confirmBulkDel = $state(false);

  const setField = async (s: Song, change: Partial<Doc>) => {
    const d = await loadDoc(s.key);
    if (d) await writeJson(lyricsPath(s.key), { ...d, ...change });
  };
  type Bulk = { label: string; title: string; when: (s: Song) => boolean; run: (s: Song) => unknown };
  const BULK: Bulk[] = [
    { label: "IA", title: "Sincroniza con IA (o transcribe las que no tienen letra)", when: (s) => !s.instrumental && !queued(s.key), run: (s) => enqueue(jobFor(s)) },
    { label: "IA rápida", title: "Sin Whisper, sobre los tiempos por línea que ya tienen", when: (s) => s.timed && !queued(s.key), run: (s) => enqueue(jobFor(s, "lineas")) },
    { label: "Buscar otra vez", title: "Vuelve a internet (la actual queda en el historial)", when: (s) => !queued(s.key), run: (s) => find(s, true) },
    { label: "Volver a la anterior", title: "Recupera la versión anterior del historial", when: (s) => !!s.history && !queued(s.key), run: restore },
    { label: "Ocultar", title: "Ocultar en el escritorio", when: (s) => !s.hidden, run: (s) => setField(s, { hidden: true }) },
    { label: "Mostrar", title: "Mostrar en el escritorio", when: (s) => !!s.hidden, run: (s) => setField(s, { hidden: false }) },
    { label: "Instrumental", title: "No buscar ni sincronizar", when: (s) => !s.instrumental, run: (s) => setField(s, { instrumental: true }) },
    { label: "No instrumental", title: "Quitar la marca de instrumental", when: (s) => !!s.instrumental, run: (s) => setField(s, { instrumental: false }) },
    { label: "Quitar desfase", title: "Desfase a 0", when: (s) => !!s.offset, run: (s) => setField(s, { offset: 0 }) },
    { label: "Borrar audio", title: "La letra se queda; la IA tendría que volver a descargar", when: (s) => !!s.bytes && !queued(s.key), run: (s) => engine(["delete", s.key, "audio"]) },
  ];

  async function bulk(a: Bulk) {
    bulkBusy = a.label;
    for (const s of pickedSongs.filter(a.when)) await Promise.resolve(a.run(s)).catch((e) => (error = `${a.label} «${s.title}»: ${e}`));
    bulkBusy = "";
    await refresh();
  }
  async function bulkRetry() {
    await retry(pickedSongs.filter((s) => s.failed).map((s) => s.key));
  }
  async function bulkDelete() {
    confirmBulkDel = false;
    bulkBusy = "Borrar";
    for (const s of pickedSongs.filter((x) => !queued(x.key))) await engine(["delete", s.key, ""]).catch((e) => (error = String(e)));
    if (picked.includes(selected)) selected = "";
    bulkBusy = "";
    await refresh();
  }

  // añadir a mano
  let addArtist = $state("");
  let addTitle = $state("");
  let addAI = $state(true);
  async function add() {
    const t = { artist: addArtist.trim(), title: addTitle.trim() };
    if (!t.artist || !t.title) return;
    const key = await find(t);
    if (!key) return;
    selected = key;
    panel = "";
    addArtist = addTitle = "";
    const s = songs.find((x) => x.key === key);
    if (addAI && !s?.timed) enqueue({ key, ...t, duration: 0, mode: "" }); // sin letra en internet: la transcribe
    else if (!s) error = `Sin letra en internet para «${t.title}». Márcala para sacarla con IA.`;
  }

  // playlists del prefetch
  let playlists = $state("");
  const failedKeys = $derived([...songs.filter((s) => s.failed).map((s) => s.key), ...orphanFailed]);
  async function openPlaylists() {
    playlists = await readText(dataPath("playlists.txt"));
    panel = "playlists";
  }
  async function retry(keys: string[]) {
    const left = (await readText(dataPath(".prefetch-failed"))).split(/\s+/).filter((k) => k && !keys.includes(k));
    await writeText(dataPath(".prefetch-failed"), left.length ? left.join("\n") + "\n" : "");
    await refresh();
  }

  /** Canción sin letra guardada (texto escrito desde cero). */
  const blank = (s: { artist: string; title: string; key: string }): Doc => ({ key: s.key, artist: s.artist, title: s.title, source: "manual", synced: false, instrumental: false, lines: [] });

  function onkey(e: KeyboardEvent) {
    if ((e.target as HTMLElement).closest("input, textarea, select")) {
      if (e.key === "Escape") (e.target as HTMLElement).blur();
      return;
    }
    if (editing) {
      if (e.key === "Escape") editing = null;
      return;
    }
    if (e.key === "/") {
      e.preventDefault();
      searchBox.focus();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
      e.preventDefault();
      picked = shown.map((s) => s.key);
    } else if (e.key === "Escape") {
      if (panel) panel = "";
      else if (confirmBulkDel) confirmBulkDel = false;
      else if (picked.length) picked = [];
      else selected = "";
    } else if (e.key.toLowerCase() === "e" && selected) void edit(selected);
  }

  onMount(() => {
    void invoke("cancel_all");
    let raf = 0;
    const frame = () => {
      now = clock() - (editing?.offset ?? 0);
      raf = requestAnimationFrame(frame);
    };
    const iv = setInterval(poll, 500);
    window.addEventListener("error", (e) => log(`${e.message} @ ${e.filename}:${e.lineno}`));
    (async () => {
      await initPaths();
      await refresh();
      void poll();
      frame();
    })();
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(iv);
    };
  });

  const playingSong = $derived(songs.find((s) => s.key === trackKey) ?? null);
</script>

<svelte:window onkeydown={onkey} />

<div class="app" class:withside={!!current}>
  <header>
    <h1>Letras</h1>
    <div class="np" class:off={!track}>
      <span class="dot" class:live={anchor.playing}></span>
      {#if track}
        <div class="np-meta">
          <b>{track.title}</b><span>{track.artist}</span>
        </div>
        {#if searching}
          <em class="tag">buscando…</em>
        {:else if playingSong}
          <button class="tag s-{syncOf(playingSong)}" onclick={() => (selected = playingSong.key)}>{SYNC_LABEL[syncOf(playingSong)]}</button>
        {:else}
          <em class="tag s-empty">sin letra</em>
          <button class="small" onclick={() => (editing = blank({ ...track!, key: trackKey }))} disabled={!trackKey}>Escribirla</button>
        {/if}
      {:else}
        <span class="muted">Spotify no está sonando</span>
      {/if}
    </div>
    <div class="tools">
      <button onclick={() => (panel = panel === "add" ? "" : "add")} class:on={panel === "add"}>＋ Añadir</button>
      <button onclick={() => (panel === "playlists" ? (panel = "") : openPlaylists())} class:on={panel === "playlists"}>
        Playlists{failedKeys.length ? ` · ${failedKeys.length} fallidas` : ""}
      </button>
      <button class="ghost" title="Releer la carpeta de datos" onclick={refresh}>↻</button>
    </div>
  </header>

  {#if job || queue.length}
    <div class="jobbar">
      <div class="spinner small"></div>
      {#if job}
        <span>IA con <b>«{job.title}»</b>: {job.label}{job.total ? ` (${job.step}/${job.total})` : ""}{job.pct != null ? ` · ${job.pct}%` : ""}</span>
      {/if}
      {#if queue.length}
        <span class="muted">· en cola: {queue.map((j) => j.title).join(", ")}</span>
        <button class="small ghost" onclick={() => (queue = [])}>Vaciar cola</button>
      {/if}
    </div>
  {/if}

  {#if panel === "add"}
    <form class="box" onsubmit={(e) => { e.preventDefault(); void add(); }}>
      <input placeholder="Artista" bind:value={addArtist} />
      <input placeholder="Canción" bind:value={addTitle} />
      <label class="check"><input type="checkbox" bind:checked={addAI} /> Sincronizar con IA si no trae tiempos</label>
      <button class="primary" disabled={!addArtist.trim() || !addTitle.trim()}>Buscar y añadir</button>
    </form>
  {:else if panel === "playlists"}
    <div class="box col">
      <p class="muted">Playlists o álbumes públicos de Spotify que se sincronizan con IA en segundo plano (cada 6 h). Una URL por línea, <code>#</code> comenta.</p>
      <textarea rows="5" bind:value={playlists}></textarea>
      <div class="tools">
        <button class="primary" onclick={async () => { await writeText(dataPath("playlists.txt"), playlists.endsWith("\n") ? playlists : playlists + "\n"); panel = ""; }}>Guardar</button>
        <button onclick={() => invoke("prefetch_now").then(() => (error = ""), (e) => (error = `prefetch: ${e}`))} title="Arranca ya el servicio letras-prefetch">Procesar ahora</button>
      </div>
      {#if failedKeys.length}
        <h3>Fallidas ({failedKeys.length}) <button class="small" onclick={() => retry(failedKeys)}>Reintentar todas</button></h3>
        <ul class="failed">
          {#each failedKeys as k (k)}
            <li><code>{k}</code> <button class="small ghost" onclick={() => retry([k])}>Reintentar</button></li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}

  <div class="filters">
    {#each FILTERS as [id, label, f] (id)}
      {@const n = count(f)}
      {#if id === "all" || n}
        <button class="chip" class:on={filter === id} onclick={() => (filter = id)}>{label} <span>{n}</span></button>
      {/if}
    {/each}
  </div>

  <div class="bar">
    <input bind:this={searchBox} class="search" placeholder="Buscar artista, canción o nota  ( / )" bind:value={q} />
    <select bind:value={sort}>
      <option value="recent">Recientes</option>
      <option value="artist">Artista</option>
      <option value="title">Canción</option>
      <option value="doubts">Más dudas</option>
      <option value="size">Más audio</option>
    </select>
    <span class="muted">{shown.length} de {songs.length} · audio {fmtBytes(totalBytes)}</span>
  </div>

  {#if picked.length}
    <div class="bar bulk">
      <b>{picked.length} seleccionadas</b>
      {#each BULK as a (a.label)}
        {@const n = pickedSongs.filter(a.when).length}
        {#if n}<button disabled={!!bulkBusy} title={a.title} onclick={() => bulk(a)}>{bulkBusy === a.label ? "…" : a.label}{n < picked.length ? ` (${n})` : ""}</button>{/if}
      {/each}
      {#if pickedSongs.some((s) => s.failed)}<button disabled={!!bulkBusy} onclick={bulkRetry}>Reintentar en prefetch</button>{/if}
      {#if confirmBulkDel}
        <button class="danger" onclick={bulkDelete}>¿Seguro? Borrar {picked.length} canciones</button>
        <button class="ghost" onclick={() => (confirmBulkDel = false)}>No</button>
      {:else}
        <button class="ghost dangertxt" disabled={!!bulkBusy} onclick={() => (confirmBulkDel = true)}>Borrar canciones</button>
      {/if}
      <span class="muted small">Shift-clic: tramo · Ctrl+A: todas las visibles · Esc: quitar</span>
      <button class="ghost" onclick={() => (picked = [])}>✕</button>
    </div>
  {/if}

  <div class="table">
    <div class="thead">
      <input type="checkbox" checked={!!shown.length && shown.every((s) => picked.includes(s.key))}
        onchange={(e) => { const keys = shown.map((s) => s.key); picked = e.currentTarget.checked ? [...new Set([...picked, ...keys])] : picked.filter((k) => !keys.includes(k)); }} />
      <span>Canción</span><span>Letra de</span><span>Sincronía</span><span>Audio</span><span>Fecha</span>
    </div>
    {#each shown as s (s.key)}
      {@const k = syncOf(s)}
      <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
      <div class="tr" class:sel={selected === s.key} class:playing={s.key === trackKey} class:hiddenrow={s.hidden} class:picked={picked.includes(s.key)}
        onmousedown={(e) => e.shiftKey && e.preventDefault()}
        onclick={(e) => (e.ctrlKey || e.metaKey || e.shiftKey ? pick(s.key, e) : (selected = s.key))}>
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
        <span onclick={(e) => e.stopPropagation()}>
          <input type="checkbox" checked={picked.includes(s.key)} onclick={(e) => pick(s.key, e)} />
        </span>
        <span class="song">
          <b>{s.title}{#if s.key === trackKey}<em class="np-mark" title="Suena en Spotify">♪</em>{/if}</b>
          <span>{s.artist}{#if s.note} · <i>{s.note}</i>{/if}</span>
        </span>
        <span>
          <em class="tag">{textLabel(s)}</em>
          {#if s.edited}<em class="tag">editada</em>{/if}
        </span>
        <span title={syncHint(s)}>
          <em class="tag s-{k}">{SYNC_LABEL[k]}</em>
          {#if s.doubts && !s.edited}<em class="tag warn" title="Líneas con palabras dudosas de Whisper">{s.doubts} dudas</em>{/if}
          {#if s.hidden}<em class="tag">oculta</em>{/if}
          {#if s.failed}<em class="tag err">falló</em>{/if}
          {#if queued(s.key)}<em class="tag busy">{job?.key === s.key ? "IA…" : "en cola"}</em>{/if}
        </span>
        <span class="audio" title={`Audio: ${s.audio.wav ? "descargado" : "no"} · voz aislada: ${s.audio.vocals ? "sí" : "no"} · Whisper: ${s.audio.whisper ? "hecho" : "no"}`}>
          <i class:on={s.audio.wav}>↓</i><i class:on={s.audio.vocals}>🎤</i><i class:on={s.audio.whisper}>W</i>
          <small>{fmtBytes(s.bytes)}</small>
        </span>
        <span class="muted">{fmtDate(s.modified)}</span>
      </div>
    {:else}
      <p class="empty">{songs.length ? "Nada con ese filtro." : "Aún no hay letras. Pon algo en Spotify o añade una canción."}</p>
    {/each}
  </div>
</div>

{#if current}
  {@const s = current}
  {@const k = syncOf(s)}
  <aside class="side">
    <header>
      <div>
        <h2>{s.title}</h2>
        <span class="muted">{s.artist}{s.duration ? ` · ${fmtTime(s.duration).replace(/\.\d+$/, "")}` : ""}</span>
      </div>
      <button class="ghost" onclick={() => (selected = "")}>✕</button>
    </header>

    <dl>
      <dt>Letra</dt>
      <dd>{SOURCE_LABEL[s.source] ?? s.source}{s.times_from ? `, con los tiempos de ${SOURCE_LABEL[s.times_from] ?? s.times_from}` : ""}{s.edited ? ", editada a mano" : ""} · {s.lines} líneas</dd>
      <dt>Sincronía</dt>
      <dd><em class="tag s-{k}">{SYNC_LABEL[k]}</em> {syncHint(s)}</dd>
      {#if s.synced_by === "ia" && SYNCED_SOURCES.has(timesFrom(s))}
        <dt></dt><dd class="muted">Ya venía sincronizada por líneas; la IA puso los tiempos palabra a palabra.</dd>
      {/if}
      <dt>Audio</dt>
      <dd>{s.audio.wav ? "Descargado" : "No descargado"}{s.audio.vocals ? " · voz aislada" : ""}{s.audio.whisper ? " · Whisper hecho" : ""} · {fmtBytes(s.bytes)}</dd>
      <dt>Versiones</dt>
      <dd>{s.history ? `${s.history} anteriores guardadas` : "ninguna anterior"}</dd>
      {#if s.doubts}<dt>Dudas</dt><dd class="warn">{s.doubts} líneas con palabras dudosas{s.edited ? " (ya revisada)" : ""}</dd>{/if}
    </dl>

    <div class="group">
      <button class="primary" onclick={() => edit(s.key)}>Editar letra (E)</button>
      <button disabled={queued(s.key) || !s.lines} onclick={() => enqueue(jobFor(s))}
        title="Whisper + alineado forzado. Mantiene el texto; tras la primera vez es casi instantáneo.">Sincronizar con IA</button>
      <button disabled={queued(s.key) || !s.timed} onclick={() => enqueue(jobFor(s, "lineas"))}
        title="Sin Whisper: parte de los tiempos por línea que ya tiene y ajusta cada palabra">IA rápida</button>
      <button disabled={queued(s.key) || !!s.lines} onclick={() => enqueue(jobFor(s))} title="Sin letra: la IA la transcribe">Transcribir con IA</button>
      <button onclick={() => find(s, true)} title="Vuelve a buscar en internet (la actual queda en el historial)">Buscar otra vez</button>
      <button disabled={!s.history || queued(s.key)} onclick={() => restore(s)}>Volver a la anterior</button>
    </div>

    <h3>Personalizar</h3>
    <label class="row">Desfase
      <button class="small" onclick={() => patch(s.key, { offset: Math.round(((s.offset ?? 0) - 0.1) * 100) / 100 })}>−0.1</button>
      <b class="mono">{(s.offset ?? 0) > 0 ? "+" : ""}{(s.offset ?? 0).toFixed(2)} s</b>
      <button class="small" onclick={() => patch(s.key, { offset: Math.round(((s.offset ?? 0) + 0.1) * 100) / 100 })}>+0.1</button>
      {#if s.offset}<button class="small ghost" onclick={() => patch(s.key, { offset: 0 })}>0</button>{/if}
    </label>
    <label class="row">Palabras
      <button class="small" onclick={() => patch(s.key, { word_offset: Math.round(((s.word_offset ?? 0) - 0.05) * 100) / 100 })}>−0.05</button>
      <b class="mono">{(s.word_offset ?? 0) > 0 ? "+" : ""}{(s.word_offset ?? 0).toFixed(2)} s</b>
      <button class="small" onclick={() => patch(s.key, { word_offset: Math.round(((s.word_offset ?? 0) + 0.05) * 100) / 100 })}>+0.05</button>
      {#if s.word_offset}<button class="small ghost" onclick={() => patch(s.key, { word_offset: 0 })}>0</button>{/if}
    </label>
    <p class="muted small">Positivo = sale más tarde. «Desfase» mueve frases y palabras; «Palabras» solo el coloreado palabra a palabra. Se aplica en el escritorio (SUPER, SUPER+N, otros monitores).</p>
    <label class="check"><input type="checkbox" checked={!!s.hidden} onchange={(e) => patch(s.key, { hidden: e.currentTarget.checked })} /> Ocultar en el escritorio</label>
    <label class="check"><input type="checkbox" checked={!!s.instrumental} onchange={(e) => patch(s.key, { instrumental: e.currentTarget.checked })} /> Instrumental (no buscar ni sincronizar)</label>
    {#key s.key}
      <textarea rows="2" placeholder="Nota (versión, qué falla…)" value={s.note ?? ""}
        onchange={(e) => patch(s.key, { note: e.currentTarget.value.trim() || undefined })}></textarea>
    {/key}

    <h3>Espacio</h3>
    <div class="group">
      <button disabled={!s.bytes || queued(s.key)} onclick={() => remove(s.key, "audio")} title="La letra se queda; la IA tendría que volver a descargar">Borrar audio ({fmtBytes(s.bytes)})</button>
      {#if s.failed}<button onclick={() => retry([s.key])}>Reintentar en prefetch</button>{/if}
      {#if confirmDel === s.key}
        <button class="danger" onclick={() => remove(s.key, "")}>¿Seguro? Borrar todo</button>
        <button class="ghost" onclick={() => (confirmDel = "")}>No</button>
      {:else}
        <button class="ghost dangertxt" disabled={queued(s.key)} onclick={() => (confirmDel = s.key)}>Borrar canción</button>
      {/if}
    </div>
  </aside>
{/if}

{#if editing}
  {#key editing}
    <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
    <div class="scrim" onclick={() => (editing = null)}></div>
    <Editor doc={editing} now={editing.key === trackKey ? now : 0} job={job}
      otherSong={job && job.key !== editing.key ? job.title : ""}
      history={songs.find((x) => x.key === editing!.key)?.history ?? 0}
      onsave={saveDoc}
      onai={async (d) => { await saveDoc(d); const s = songs.find((x) => x.key === d.key); if (s) enqueue(jobFor(s)); }}
      onfast={async (d) => { await saveDoc(d); const s = songs.find((x) => x.key === d.key); if (s) enqueue(jobFor(s, "lineas")); }}
      onrestore={() => { const s = songs.find((x) => x.key === editing!.key); if (s) void restore(s); }}
      onseek={(t) => editing?.key === trackKey && seek(t)}
      onclose={() => (editing = null)} />
  {/key}
{/if}

{#if error}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
  <div class="toast" onclick={() => (error = "")} title="Clic para cerrar">{error}</div>
{/if}

<style>
  :global(html, body) {
    margin: 0;
    height: 100%;
    background: #0e0c16;
    color: #fff;
    font-family: Inter, system-ui, sans-serif;
    font-size: 14px;
  }
  :global(button) {
    font: inherit;
    font-size: 13px;
    color: #fff;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 6px 12px;
    cursor: pointer;
    transition: background 0.15s, transform 0.1s;
  }
  :global(button:hover:not(:disabled)) { background: rgba(255, 255, 255, 0.18); }
  :global(button:active:not(:disabled)) { transform: scale(0.96); }
  :global(button:disabled) { opacity: 0.4; cursor: default; }
  :global(button.primary) { background: linear-gradient(135deg, #ff3d7f, #7a3dff); border-color: transparent; }
  :global(button.on) { background: rgba(255, 255, 255, 0.85); color: #111; }
  :global(button.ghost) { background: none; border-color: transparent; padding: 4px 8px; }
  :global(button.small) { padding: 2px 9px; font-size: 12px; }
  :global(input:not([type="range"]):not([type="color"]):not([type="checkbox"]), select, textarea) {
    font: inherit;
    font-size: 13px;
    color: #fff;
    background: rgba(0, 0, 0, 0.35);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 8px;
    padding: 6px 9px;
    box-sizing: border-box;
  }
  :global(select option) { background: #1a1726; }
  :global(input[type="checkbox"]) { accent-color: #ff3d7f; }
  :global(.hint) { font-size: 13px; opacity: 0.75; margin: 0; }

  .app {
    height: 100vh;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 14px 18px;
    box-sizing: border-box;
    transition: padding-right 0.3s;
  }
  .app.withside { padding-right: calc(min(400px, 45vw) + 18px); }
  header { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  h1 { margin: 0; font-size: 22px; font-weight: 800; }
  h2 { margin: 0; font-size: 18px; }
  h3 { margin: 16px 0 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.6; display: flex; gap: 8px; align-items: center; }
  .tools { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-left: auto; }
  .muted { opacity: 0.6; }
  .small { font-size: 12px; }
  .mono { font-variant-numeric: tabular-nums; min-width: 64px; text-align: center; }
  code { font-size: 12px; opacity: 0.85; }

  .np {
    display: flex; align-items: center; gap: 10px; min-width: 0; flex: 1;
    padding: 6px 12px; border-radius: 999px; background: rgba(255, 255, 255, 0.05);
  }
  .np.off { opacity: 0.6; }
  .np-meta { display: flex; gap: 8px; align-items: baseline; min-width: 0; overflow: hidden; white-space: nowrap; }
  .np-meta span { opacity: 0.65; overflow: hidden; text-overflow: ellipsis; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #555; flex: none; }
  .dot.live { background: #1ed760; box-shadow: 0 0 8px #1ed760; }

  .jobbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 12px; border-radius: 10px; background: rgba(255, 61, 127, 0.12); border: 1px solid rgba(255, 61, 127, 0.3); }
  .box { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; padding: 12px; border-radius: 12px; background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); }
  .box.col { flex-direction: column; align-items: stretch; }
  .box p { margin: 0; }
  .box textarea { width: 100%; resize: vertical; font-family: "JetBrains Mono", monospace; font-size: 12px; }
  .failed { margin: 0; padding: 0; list-style: none; max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
  .check { display: flex; gap: 6px; align-items: center; font-size: 13px; }

  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip span { opacity: 0.6; margin-left: 4px; font-variant-numeric: tabular-nums; }
  .chip.on span { opacity: 0.8; }
  .bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .search { flex: 1; min-width: 180px; }
  .bulk { flex-wrap: wrap; padding: 6px 10px; border-radius: 10px; background: rgba(122, 61, 255, 0.15); }

  .table { flex: 1; overflow-y: auto; border-radius: 12px; border: 1px solid rgba(255, 255, 255, 0.07); }
  .thead, .tr {
    display: grid;
    grid-template-columns: 28px minmax(160px, 2.2fr) minmax(90px, 1fr) minmax(150px, 1.6fr) 120px 72px;
    gap: 10px;
    align-items: center;
    padding: 7px 12px;
  }
  .thead { position: sticky; top: 0; background: #16131f; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.8; z-index: 1; }
  .tr { border-top: 1px solid rgba(255, 255, 255, 0.04); cursor: pointer; }
  .tr:hover { background: rgba(255, 255, 255, 0.04); }
  .tr.picked { background: rgba(122, 61, 255, 0.1); }
  .tr.sel { background: rgba(255, 61, 127, 0.12); }
  .tr.playing { box-shadow: inset 3px 0 #1ed760; }
  .tr.hiddenrow .song { opacity: 0.5; }
  .song { display: flex; flex-direction: column; min-width: 0; }
  .song b, .song span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .song span { opacity: 0.6; font-size: 13px; }
  .np-mark { color: #1ed760; font-style: normal; margin-left: 6px; }
  .audio { display: flex; gap: 6px; align-items: center; }
  .audio i { font-style: normal; opacity: 0.2; font-size: 12px; }
  .audio i.on { opacity: 1; }
  .audio small { opacity: 0.55; font-variant-numeric: tabular-nums; }
  .empty { text-align: center; opacity: 0.6; padding: 30px; }

  .tag {
    display: inline-block;
    font-style: normal;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.1);
    border: none;
    margin: 1px 2px 1px 0;
    white-space: nowrap;
  }
  button.tag { cursor: pointer; }
  .s-origin { background: rgba(30, 215, 96, 0.2); color: #7cf2a6; }
  .s-ia { background: rgba(122, 61, 255, 0.3); color: #c9b2ff; }
  .s-ia-fast { background: rgba(0, 194, 255, 0.2); color: #8fe3ff; }
  .s-manual { background: rgba(255, 255, 255, 0.18); }
  .s-none, .s-empty { background: rgba(255, 194, 61, 0.18); color: #ffd47a; }
  .s-instrumental { background: rgba(255, 255, 255, 0.08); color: #bbb; }
  .tag.warn, dd.warn { color: #ffc23d; }
  .tag.err { background: rgba(255, 60, 60, 0.25); color: #ff9a9a; }
  .tag.busy { background: rgba(255, 61, 127, 0.3); color: #ffb3cf; }

  .side {
    position: fixed; top: 0; right: 0; bottom: 0;
    width: min(400px, 45vw);
    box-sizing: border-box;
    overflow-y: auto;
    padding: 16px;
    background: #141120;
    border-left: 1px solid rgba(255, 255, 255, 0.08);
    animation: slide 0.3s cubic-bezier(0.2, 0.9, 0.3, 1);
  }
  .side header { justify-content: space-between; align-items: flex-start; flex-wrap: nowrap; }
  .side textarea { width: 100%; resize: vertical; margin-top: 6px; }
  .side .row { display: flex; gap: 6px; align-items: center; font-size: 13px; }
  .side p { margin: 4px 0; }
  @keyframes slide { from { transform: translateX(30px); opacity: 0; } }
  dl { display: grid; grid-template-columns: 82px 1fr; gap: 6px 10px; margin: 16px 0 12px; font-size: 13px; }
  dt { opacity: 0.55; }
  dd { margin: 0; }
  .group { display: flex; gap: 6px; flex-wrap: wrap; }
  .danger { background: #b3203f; border-color: transparent; }
  .dangertxt { color: #ff8fa3; }

  .scrim { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5); z-index: 9; }
  .toast {
    position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%);
    max-width: calc(100vw - 40px); box-sizing: border-box;
    padding: 10px 16px; border-radius: 12px;
    background: rgba(140, 16, 40, 0.92); cursor: pointer; z-index: 20; user-select: text;
  }
  .spinner { border-radius: 50%; border: 2px solid rgba(255, 255, 255, 0.15); border-top-color: #fff; animation: spin 0.8s linear infinite; }
  .spinner.small { width: 14px; height: 14px; flex: none; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
