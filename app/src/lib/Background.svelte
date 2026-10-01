<script lang="ts">
  import type { Settings } from "./settings";

  let { s, art }: { s: Settings; art?: string } = $props();
  // Firefox y mpv dan carátulas file:// que el WebView no puede leer: entonces, aurora
  const cover = $derived(s.bg === "cover" && art?.startsWith("http") ? art : null);
</script>

{#if s.bg !== "transparent"}
  <div class="bg" style:background={s.bg === "solid" ? s.c1 : "#07060b"}>
    {#if cover}
      {#key cover}
        <img class="cover" src={cover} alt="" />
      {/key}
    {:else if s.bg !== "solid"}
      <div class="blob" style:background={s.c1} style:--x="-20%" style:--y="-25%" style:animation-duration="23s"></div>
      <div class="blob" style:background={s.c2} style:--x="45%" style:--y="30%" style:animation-duration="31s"></div>
      <div class="blob" style:background={s.c3} style:--x="5%" style:--y="55%" style:animation-duration="27s"></div>
    {/if}
    <div class="dim" style:background="rgba(0,0,0,{s.dim})"></div>
    <div class="grain"></div>
  </div>
{/if}

<style>
  .bg {
    position: fixed;
    inset: 0;
    overflow: hidden;
    z-index: -1;
  }
  .cover {
    position: absolute;
    inset: -30%;
    width: 160%;
    height: 160%;
    object-fit: cover;
    filter: blur(70px) saturate(1.8) brightness(0.8);
    animation: spin 90s linear infinite, fade 1.2s ease;
  }
  .blob {
    position: absolute;
    width: 85vmax;
    height: 85vmax;
    left: var(--x);
    top: var(--y);
    border-radius: 50%;
    filter: blur(90px);
    opacity: 0.75;
    mix-blend-mode: screen;
    animation: drift ease-in-out infinite alternate;
  }
  .dim,
  .grain {
    position: absolute;
    inset: 0;
  }
  /* ruido fino para que el degradado no haga bandas */
  .grain {
    opacity: 0.07;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='160' height='160'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }
  @keyframes drift {
    33% { transform: translate(12vw, -8vh) scale(1.15); }
    66% { transform: translate(-10vw, 10vh) scale(0.9); }
    100% { transform: translate(6vw, 4vh) scale(1.05); }
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }
  @keyframes fade {
    from { opacity: 0; }
  }
</style>
