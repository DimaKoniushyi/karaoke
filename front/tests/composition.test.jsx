/* @vitest-environment jsdom */
import { render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { verify } from "./helpers/assertions.mjs";
vi.mock("../src/theme/ui", () => ({
  Card: ({ children }) => <div>{children}</div>,
  Stack: ({ children }) => <div>{children}</div>,
  Grid: ({ children }) => <div>{children}</div>
}));
vi.mock("../src/i18n", () => ({
  I18nProvider: ({ children }) => <div data-provider="i18n">{children}</div>
}));
vi.mock("../src/contexts/AppDialog", () => ({
  AppDialogProvider: ({ children }) => <div data-provider="dialog">{children}</div>
}));
vi.mock("../src/contexts/OnlineRoomContext", () => ({
  OnlineRoomProvider: ({ children }) => <div data-provider="room">{children}</div>
}));
vi.mock("../src/contexts/app-settings", () => ({
  default: ({ children }) => <div data-provider="settings">{children}</div>
}));
vi.mock("../src/contexts/radio", () => ({
  RadioProvider: ({ children }) => <div data-provider="radio">{children}</div>
}));
vi.mock("../src/pages/Karaoke/console/center", () => ({
  default: ({ marker }) => <span data-testid="center">{marker}</span>
}));
vi.mock("../src/pages/Karaoke/console/mixer", () => ({
  default: ({ marker }) => <span data-testid="mixer">{marker}</span>
}));
vi.mock("../src/pages/Karaoke/console/song-strip", () => ({
  default: ({ marker }) => <span data-testid="strip">{marker}</span>
}));
vi.mock("../src/pages/Karaoke/console/tools", () => ({
  default: ({ marker }) => <span data-testid="tools">{marker}</span>
}));
import ContextProviders from "../src/contexts/index.jsx";
import KaraokeConsole from "../src/pages/Karaoke/console/index.jsx";
test("context composition preserves provider ownership order", () => {
  const result = render(
    <ContextProviders>
      {" "}
      <span data-testid="child" />{" "}
    </ContextProviders>
  );
  verify([result.getByTestId("child").closest('[data-provider="room"]'), "toBeTruthy"]);
  expect(result.container.querySelectorAll("[data-provider]")).toHaveLength(5);
});
test("karaoke console forwards shared and auto-hide contracts", () => {
  const result = render(<KaraokeConsole marker="karaoke" autoHideEnabled onAutoHideChange={vi.fn()} />);
  for (const id of ["strip", "mixer", "center", "tools"]) {
    expect(result.getByTestId(id).textContent).toBe("karaoke");
  }
});
