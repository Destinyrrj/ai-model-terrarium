import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { RunSummary, TickRecord } from "../../api/types";
import { TickProgress } from "./TickProgress";

const cleanups: Array<() => void> = [];

function renderProgress(run: RunSummary, ticks: TickRecord[] = []): HTMLDivElement {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  cleanups.push(() => root.unmount());
  act(() => root.render(<TickProgress run={run} ticks={ticks} />));
  return container;
}

function run(overrides: Partial<RunSummary>): RunSummary {
  return { name: "run", status: "running", raw: {}, ...overrides };
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) act(cleanup);
  document.body.replaceChildren();
});

describe("TickProgress invocation semantics", () => {
  it("never treats max_ticks as an absolute tick target", () => {
    const container = renderProgress(run({ current_tick: 25, max_ticks: 10 }));
    expect(container.querySelector(".progress-track")).toBeNull();
    expect(container.textContent).toContain("tick 25");
    expect(container.textContent).toContain("Бюджет текущего invocation: +10 commits");
    expect(container.textContent).not.toContain("250%");
  });

  it("uses explicit invocation bounds for percentages and generation positions", () => {
    const container = renderProgress(
      run({ current_tick: 25, max_ticks: 10, invocation_start_tick: 20, invocation_target_tick: 30 }),
      [
        { tick: 21, generation: 1, raw: {} },
        { tick: 23, generation: 2, raw: {} },
      ],
    );
    expect(container.querySelector<HTMLElement>(".progress-fill")?.style.width).toBe("50%");
    expect(container.textContent).toContain("5 / 10 invocation commits · 50%");
    const markers = container.querySelectorAll<HTMLElement>(".generation-marker");
    expect(markers[0]?.style.left).toBe("10%");
    expect(markers[1]?.style.left).toBe("30%");
  });
});
