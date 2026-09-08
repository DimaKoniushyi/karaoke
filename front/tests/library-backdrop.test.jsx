/* @vitest-environment jsdom */
import fs from "node:fs";
import { act, render } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import LibraryBackdrop from "../src/pages/Library/backdrop/index.jsx";

test("library backdrop is decorative and cannot intercept controls", () => {
  const { container } = render(<LibraryBackdrop />);
  const backdrop = container.firstElementChild;
  expect(backdrop.getAttribute("aria-hidden")).toBe("true");
  expect(backdrop.style.position).toBe("fixed");
  expect(backdrop.style.pointerEvents).toBe("none");
  expect(backdrop.style.mixBlendMode).toBe("normal");
  expect(backdrop.style.filter).toBe("none");
});

test("stays hidden until the runtime confirms QFT_READY, then fades in", () => {
  // A freshly created iframe document paints the browser's own opaque white
  // default background for a moment before its own transparent html/body
  // style takes effect. On this fixed, full-viewport layer that white used
  // to cover the real backdrop image underneath for a beat on every mount
  // (app startup, and every time Library remounts after returning from
  // Karaoke) -- it must stay invisible until the runtime says it's ready.
  const { container } = render(<LibraryBackdrop />);
  const backdrop = container.firstElementChild;
  const frame = container.querySelector("iframe");
  expect(backdrop.style.opacity).toBe("0");

  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", { data: { type: "QFT_READY" }, source: frame.contentWindow })
    );
  });

  expect(backdrop.style.opacity).toBe("1");
});

test("library backdrop keeps its packaged runtime URL stable and query-free", () => {
  const { container, rerender } = render(<LibraryBackdrop />);
  const source = container.querySelector("iframe").srcdoc;
  expect(source).toMatch(/<script type="module" src="[^"]*qftRuntime[^"]*"><\/script>/);
  expect(source).not.toMatch(/[?&]v=29/);
  rerender(<LibraryBackdrop />);
  expect(container.querySelector("iframe").srcdoc).toBe(source);
});

test("library backdrop keeps black WebGL pixels transparent over the theme artwork", () => {
  const runtime = fs.readFileSync("src/pages/Library/backdrop/qftRuntime.js", "utf8");
  const component = fs.readFileSync("src/pages/Library/backdrop/index.jsx", "utf8");
  expect(runtime).toContain("float overlayAlpha = clamp(visibleLight * 1.35, 0.0, 1.0);");
  expect(runtime).not.toContain("gl_FragColor = vec4(detailLift, 1.0);");
  expect(runtime).toContain("themeBackdrop.style.backgroundImage = backgroundImage;");
  expect(runtime).toContain("element.style.backgroundColor = backgroundColor;");
  expect(runtime).toContain("backdropTargetX");
  expect(component).toContain("dark.webp?inline");
  expect(component).toContain('backgroundImage: `url("${THEME_BACKDROPS[theme]');
  expect(component).not.toContain("getComputedStyle(themeBackground).backgroundImage");
});

test("library backdrop forwards pointer movement to the visualizer runtime", () => {
  const scheduledFrames = [];
  const requestFrame = vi
    .spyOn(window, "requestAnimationFrame")
    .mockImplementation((callback) => {
      scheduledFrames.push(callback);
      return scheduledFrames.length;
    });
  const cancelFrame = vi.spyOn(window, "cancelAnimationFrame").mockImplementation(() => {});

  try {
    const { container, unmount } = render(<LibraryBackdrop />);
    const frame = container.querySelector("iframe");
    const postMessage = vi.spyOn(frame.contentWindow, "postMessage");

    act(() => {
      window.dispatchEvent(new PointerEvent("pointermove", { clientX: 256, clientY: 192 }));
      scheduledFrames.at(-1)?.(16);
    });

    expect(postMessage).toHaveBeenCalledWith(
      {
        type: "QFT_POINTER",
        x: (256 / window.innerWidth) * 2 - 1,
        y: (192 / window.innerHeight) * 2 - 1
      },
      "*"
    );
    unmount();
  } finally {
    requestFrame.mockRestore();
    cancelFrame.mockRestore();
  }
});

test("library backdrop watches the theme attribute instead of polling it every frame", () => {
  // Regression test: updateTheme() used to run inside the rAF loop, reading
  // documentElement's data-theme attribute up to 60 times a second just to
  // catch the rare case where the user actually toggles it. A
  // MutationObserver reacts to that attribute changing instead, so this
  // checks the observer is wired to the right target/attribute and torn
  // down on unmount, without needing to reach into the (mocked) WebGL scene.
  const observe = vi.fn();
  const disconnect = vi.fn();
  const OriginalMutationObserver = globalThis.MutationObserver;
  class FakeMutationObserver {
    observe(...args) {
      observe(...args);
    }

    disconnect() {
      disconnect();
    }
  }
  globalThis.MutationObserver = FakeMutationObserver;
  try {
    const { unmount } = render(<LibraryBackdrop />);
    expect(observe).toHaveBeenCalledWith(
      document.documentElement,
      expect.objectContaining({ attributes: true, attributeFilter: ["data-theme"] })
    );
    unmount();
    expect(disconnect).toHaveBeenCalledOnce();
  } finally {
    globalThis.MutationObserver = OriginalMutationObserver;
  }
});

test("library backdrop cleans up its window message listener on unmount", () => {
  const add = vi.spyOn(window, "addEventListener");
  const remove = vi.spyOn(window, "removeEventListener");
  const { unmount } = render(<LibraryBackdrop />);
  const messageCall = add.mock.calls.find(([type]) => type === "message");
  expect(messageCall).toBeDefined();
  unmount();
  expect(remove).toHaveBeenCalledWith("message", messageCall[1]);
  add.mockRestore();
  remove.mockRestore();
});

test("library backdrop explicitly disposes iframe audio, RAF and WebGL resources", () => {
  const runtime = fs.readFileSync("src/pages/Library/backdrop/qftRuntime.js", "utf8");
  const { container, unmount } = render(<LibraryBackdrop />);
  const frame = container.querySelector("iframe");
  const postMessage = vi.spyOn(frame.contentWindow, "postMessage");

  unmount();

  expect(postMessage).toHaveBeenCalledWith({ type: "QFT_DISPOSE" }, "*");
  expect(runtime).toContain("window.__QFT_DISPOSE__ = dispose;");
  expect(runtime).toContain("AUDIO.ctx?.close?.()");
  expect(runtime).toContain("renderer.forceContextLoss?.()");
  expect(runtime).toContain("cancelAnimationFrame(id)");
  expect(runtime).toContain("target.removeEventListener?.(type, handler, options)");
});

test("library backdrop releases its microphone and WebGL iframe while the app is minimized", () => {
  let notify;
  globalThis.electronAPI = {
    isElectron: true,
    onHardwareSuspensionChange: (callback) => {
      notify = callback;
      return () => {};
    }
  };
  const { container } = render(<LibraryBackdrop />);
  const frame = container.querySelector("iframe");
  const postMessage = vi.spyOn(frame.contentWindow, "postMessage");

  act(() => notify(true));
  expect(postMessage).toHaveBeenCalledWith({ type: "QFT_DISPOSE" }, "*");
  expect(container.querySelector("iframe")).toBeNull();

  act(() => notify(false));
  expect(container.querySelector("iframe")).not.toBeNull();
  delete globalThis.electronAPI;
});
