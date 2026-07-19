import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../EChart", () => ({
  baseAxis: {},
  baseGrid: {},
  baseTooltip: {},
  chartTheme: {
    blue: "blue",
    gold: "gold",
    olive: "olive",
    orange: "orange",
    pink: "pink",
    muted: "muted",
    ink: "ink",
    dark: "dark",
    palette: ["blue", "gold"],
  },
  EChart: ({ ariaLabel, option }: { ariaLabel: string; option: unknown }) => (
    <div data-chart-label={ariaLabel} data-chart-option={JSON.stringify(option)} />
  ),
}));

import { SurvivalCurves, TokenChart } from "./MetricCharts";

const cleanups: Array<() => void> = [];

function renderTokenChart(): HTMLDivElement {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  cleanups.push(() => root.unmount());
  act(() => root.render(<TokenChart data={{
    series: [{ tick: 3_001, model: "m", input_tokens: 12, output_tokens: 4, reasoning_tokens: 2 }],
    window: {
      total_ticks: null,
      total_ticks_lower_bound: 5_001,
      returned_ticks: 2_000,
      window_truncated: true,
      totals_scope: "window",
      first_tick: 3_001,
      last_tick: 5_000,
    },
  }} />));
  return container;
}

afterEach(() => {
  for (const cleanup of cleanups.splice(0)) act(cleanup);
  document.body.replaceChildren();
});

describe("TokenChart bounded window", () => {
  it("labels truncated series and totals as window-scoped", () => {
    const container = renderTokenChart();
    const text = container.textContent?.replace(/\s+/g, " ") ?? "";
    expect(text).toContain("Последнее окно: 2 000 ticks с token usage; в запуске не менее 5 001");
    expect(text).toContain("не ко всему запуску");
    expect(text).toContain("Итоги по отображённому окну");
    expect(container.querySelector("[data-chart-label]")?.getAttribute("data-chart-label")).toContain("последним 2000; в запуске не менее 5001");
  });
});

describe("SurvivalCurves indexed series", () => {
  it("preserves first-match and sparse-cell behavior without repeated finds", () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    cleanups.push(() => root.unmount());
    act(() => root.render(<SurvivalCurves data={{ points: [
      { generation: 1, fact_id: "f1", basis: "authored", channel: "written", survival_rate: 0.1, total: 1 },
      { generation: 1, fact_id: "f1", basis: "authored", channel: "written", survival_rate: 0.9, total: 2 },
      { generation: 3, fact_id: "f1", basis: "authored", channel: "written", survival_rate: 0.3, total: 3 },
      { generation: 2, fact_id: "f2", basis: "authored", channel: "written", survival_rate: 0.2, total: 4 },
    ] }} />));

    const rawOption = container.querySelector("[data-chart-option]")?.getAttribute("data-chart-option");
    expect(rawOption).toBeTruthy();
    const option = JSON.parse(rawOption ?? "{}") as { series?: Array<{ name: string; data: Array<number | null> }> };
    expect(option.series?.find((series) => series.name === "f1")?.data).toEqual([0.1, null, 0.3]);
    expect(option.series?.find((series) => series.name === "f2")?.data).toEqual([null, 0.2, null]);
    expect(container.textContent?.replace(/\s+/g, " ")).toContain("n = 10");
  });
});
