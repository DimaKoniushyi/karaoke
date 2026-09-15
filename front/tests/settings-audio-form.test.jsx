/* @vitest-environment jsdom */
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import SettingsPage from "./helpers/settings-page";

test("audio settings group devices, actions and levels semantically", () => {
  const monitor = vi.fn();
  render(
    <SettingsPage
      tab="audio"
      settings={{
        app: { form: {} },
        audio: {
          values: { volume: 1, noise_suppression: 0.35, monitoring_enabled: false },
          options: { inputs: [], outputs: [] },
          busy: false,
          level: 0,
          update: vi.fn(),
          speaker: vi.fn(),
          monitor
        },
        radio: {}
      }}
    />
  );

  // Noise suppression lives on the Processing tab; Sound keeps only the
  // realtime monitoring volume slider.
  expect(screen.getAllByRole("slider")).toHaveLength(1);
  const meter = screen.getByRole("meter", { name: "Уровень микрофона" });
  expect(meter.getAttribute("aria-valuenow")).toBe("0");
  expect(meter.querySelector("path")).not.toBeNull();
  expect(meter.querySelector("rect")).toBeNull();
  expect(screen.getByRole("button", { name: "Проверить звук" })).not.toBeNull();
  fireEvent.click(screen.getByRole("switch", { name: "Мониторинг" }));
  expect(monitor).toHaveBeenCalledOnce();
});
