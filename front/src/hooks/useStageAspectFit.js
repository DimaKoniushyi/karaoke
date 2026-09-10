import { useEffect } from "react";

// Keeps a 16:9 stage letterboxed inside its parent (via ResizeObserver,
// falling back to a window resize listener), and reports how much extra
// nav height that letterboxing needs through a CSS custom property on the
// nearest ".karaoke-app-shell" ancestor. Generic sizing math -- not tied to
// Karaoke beyond that one shell class name.
export default function useStageAspectFit(ref) {
  useEffect(() => {
    const shell = globalThis.document?.querySelector?.(".karaoke-app-shell");
    const stage = ref.current;
    const main = stage?.parentElement;
    if (!shell || !main || !stage) return;

    const sync = () => {
      const finite = (value) => {
        const number = Number(value);
        return Number.isFinite(number) ? Math.max(0, number) : 0;
      };
      const mainWidth = finite(main.clientWidth);
      const mainHeight = finite(main.clientHeight);
      const stageWidth = finite(stage.clientWidth);
      const stageHeight = finite(stage.clientHeight);
      const nav = finite(
        parseFloat(globalThis.getComputedStyle(shell).getPropertyValue("--karaoke-nav-extra"))
      );
      shell.style.setProperty(
        "--karaoke-nav-extra",
        `${Math.max(0, mainHeight + nav - (mainWidth * 9) / 16)}px`
      );
      stage.style.setProperty(
        "--karaoke-video-width",
        `${Math.ceil(Math.max(stageWidth, (stageHeight * 16) / 9)) + 2}px`
      );
      stage.style.setProperty(
        "--karaoke-video-height",
        `${Math.ceil(Math.max(stageHeight, (stageWidth * 9) / 16)) + 2}px`
      );
    };
    const observer = globalThis.ResizeObserver ? new globalThis.ResizeObserver(sync) : null;
    observer?.observe(main);
    observer?.observe(stage);
    if (!observer) globalThis.addEventListener?.("resize", sync);
    sync();
    return () => {
      observer?.disconnect();
      globalThis.removeEventListener?.("resize", sync);
      ["--karaoke-nav-extra"].forEach((name) => shell.style.removeProperty(name));
      ["--karaoke-video-width", "--karaoke-video-height"].forEach((name) =>
        stage.style.removeProperty(name)
      );
    };
  }, [ref]);
}
