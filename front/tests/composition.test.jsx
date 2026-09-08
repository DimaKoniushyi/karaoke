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
  default: () => <span data-testid="center" />
}));
vi.mock("../src/pages/Karaoke/console/mixer", () => ({
  default: () => <span data-testid="mixer" />
}));
vi.mock("../src/pages/Karaoke/console/song-strip", () => ({
  default: () => <span data-testid="strip" />
}));
vi.mock("../src/pages/Karaoke/console/tools", () => ({
  default: () => <span data-testid="tools" />
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
test("karaoke console composes every panel around the grouped transport contract", () => {
  const result = render(<KaraokeConsole transport={{ seekTo: vi.fn() }} />);
  for (const id of ["strip", "mixer", "center", "tools"]) {
    expect(result.getByTestId(id)).not.toBeNull();
  }
});
