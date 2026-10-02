<script lang="ts">
  import type { Settings } from "./settings";

  let { s, art }: { s: Settings; art?: string } = $props();
  // Firefox y mpv dan carátulas file:// que el WebView no puede leer: entonces, aurora
  const cover = $derived(s.bg === "cover" && art?.startsWith("http") ? art : null);
</script>

<!-- Todo estático y sin filter/blend: el desenfoque animado se comía ~5 GB de VRAM y dejaba a la IA sin GPU -->
{#if s.bg !== "transparent"}
  <div
    class="bg"
    style:background={s.bg === "solid"
      ? s.c1
      : `radial-gradient(circle at 15% 10%, ${s.c1}aa, transparent 55%), radial-gradient(circle at 85% 60%, ${s.c2}aa, transparent 55%), radial-gradient(circle at 30% 95%, ${s.c3}aa, transparent 55%), #07060b`}
  >
    {#if cover}
      <img class="cover" src={cover} alt="" />
    {/if}
    <div class="dim" style:background="rgba(0,0,0,{s.dim})"></div>
  </div>
{/if}

<style>
  .bg {
    position: fixed;
    inset: 0;
    overflow: hidden;
    z-index: -1;
  }
  .cover,
  .dim {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
  }
  .cover {
    object-fit: cover;
    opacity: 0.45;
  }
</style>
