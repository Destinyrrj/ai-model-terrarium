import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { SanitizedText } from "./Primitives";

const cleanups: Array<() => void> = [];

function renderText(value: unknown, limit?: number): HTMLDivElement {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  cleanups.push(() => root.unmount());
  act(() => root.render(<SanitizedText value={value} limit={limit} />));
  return container;
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) act(cleanup);
  document.body.replaceChildren();
});

describe("SanitizedText", () => {
  it("renders hostile markup as text and replaces forbidden control characters", () => {
    const container = renderText("safe\u0000<script>alert(1)</script>");
    expect(container.textContent).toBe("safe�<script>alert(1)</script>");
    expect(container.querySelector("script")).toBeNull();
  });

  it("truncates after sanitizing", () => {
    const container = renderText("ab\u0000cdef", 4);
    expect(container.textContent).toBe("ab�c… [truncated]");
  });
});
