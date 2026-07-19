import type { EChartsOption } from "echarts";
import { memo, useMemo, useState } from "react";
import { asBoolean, asNumber, asRecord, asString, firstNumber, unwrapArray } from "../../api/normalize";
import { formatInteger, formatPercent, humanize } from "../../lib/format";
import { baseAxis, baseGrid, baseTooltip, chartTheme, EChart } from "../EChart";
import { EmptyState } from "../Primitives";

interface GaugeMetric {
  key: string;
  label: string;
  used: number;
  limit: number;
}

function metricValue(record: Record<string, unknown>, containers: Record<string, unknown>[], keys: string[]): number | undefined {
  for (const container of [record, ...containers]) {
    const found = firstNumber(container, keys);
    if (found !== undefined) return found;
  }
  return undefined;
}

function budgetMetrics(value: unknown): GaugeMetric[] {
  const root = asRecord(value);
  const used = asRecord(root.used ?? root.usage ?? root.current);
  const limits = asRecord(root.limits ?? root.limit ?? root.config ?? root.budget);
  const specs = [
    { key: "calls", label: "Calls", used: ["calls", "call_count", "used_calls"], limit: ["max_calls", "calls_limit", "calls"] },
    { key: "input", label: "Input tokens", used: ["input_tokens", "used_input_tokens"], limit: ["max_input_tokens", "input_tokens_limit", "input_tokens"] },
    { key: "output", label: "Output tokens", used: ["output_tokens", "used_output_tokens"], limit: ["max_output_tokens", "output_tokens_limit", "output_tokens"] },
    { key: "failures", label: "Failures", used: ["failures", "failure_count", "used_failures"], limit: ["max_failures", "failures_limit", "failures"] },
  ];
  return specs.flatMap((spec) => {
    const usedValue = metricValue(root, [used], spec.used);
    const limitValue = metricValue(root, [limits], spec.limit);
    return usedValue !== undefined && limitValue !== undefined
      ? [{ key: spec.key, label: spec.label, used: usedValue, limit: limitValue }]
      : [];
  });
}

export const BudgetGauges = memo(function BudgetGauges({ data }: { data: unknown }): React.JSX.Element {
  const metrics = budgetMetrics(data);
  const option = useMemo<EChartsOption>(() => ({
    animationDuration: 350,
    series: metrics.map((metric, index) => {
      const col = index % 2;
      const row = Math.floor(index / 2);
      const ratio = metric.limit > 0 ? metric.used / metric.limit : metric.used > 0 ? 1 : 0;
      return {
        type: "gauge" as const,
        center: [`${25 + col * 50}%`, `${29 + row * 48}%`],
        radius: "34%",
        startAngle: 210,
        endAngle: -30,
        min: 0,
        max: 100,
        splitNumber: 4,
        progress: { show: true, width: 8, itemStyle: { color: ratio > 0.9 ? chartTheme.orange : chartTheme.blue } },
        axisLine: { lineStyle: { width: 8, color: [[1, "rgba(128,148,142,.18)"]] } },
        axisTick: { show: false },
        splitLine: { show: false },
        axisLabel: { show: false },
        pointer: { show: false },
        anchor: { show: false },
        title: { offsetCenter: [0, "76%"], color: chartTheme.muted, fontSize: 11 },
        detail: {
          valueAnimation: true,
          offsetCenter: [0, "4%"],
          color: chartTheme.ink,
          fontFamily: "IBM Plex Mono, ui-monospace, monospace",
          fontSize: 18,
          formatter: () => `${Math.round(ratio * 100)}%`,
        },
        data: [{ value: Math.min(100, ratio * 100), name: metric.label }],
      };
    }),
  }), [metrics]);
  if (!metrics.length) return <EmptyState title="Бюджет ещё не рассчитан" detail="Нужен checkpoint и лимиты из снапшота конфигурации." />;
  return (
    <div>
      <EChart option={option} height={330} ariaLabel="Использование бюджетов вызовов, токенов и ошибок" />
      <div className="gauge-values">
        {metrics.map((metric) => <span key={metric.key}><b>{metric.label}</b><code>{formatInteger(metric.used)} / {formatInteger(metric.limit)}</code></span>)}
      </div>
    </div>
  );
});

interface TokenRow {
  tick: number;
  model: string;
  input: number;
  output: number;
  reasoning: number;
}

function tokenRows(value: unknown): TokenRow[] {
  return unwrapArray(value, ["series", "ticks", "usage", "tokens", "items", "data", "results"]).flatMap((item) => {
    const row = asRecord(item);
    const tick = firstNumber(row, ["tick", "tick_id"]);
    if (tick === undefined) return [];
    return [{
      tick,
      model: asString(row.model) ?? asString(row.model_id) ?? asString(row.provider) ?? "model",
      input: firstNumber(row, ["input_tokens", "input"]) ?? 0,
      output: firstNumber(row, ["output_tokens", "output"]) ?? 0,
      reasoning: firstNumber(row, ["reasoning_tokens", "reasoning"]) ?? 0,
    }];
  });
}

export const TokenChart = memo(function TokenChart({ data }: { data: unknown }): React.JSX.Element {
  const rows = tokenRows(data);
  const ticks = [...new Set(rows.map((row) => row.tick))].sort((a, b) => a - b);
  const window = asRecord(asRecord(data).window);
  const returnedTicks = firstNumber(window, ["returned_ticks"]);
  const totalTicks = firstNumber(window, ["total_ticks"]);
  const totalTicksLowerBound = firstNumber(window, ["total_ticks_lower_bound"]);
  const firstTick = firstNumber(window, ["first_tick"]);
  const lastTick = firstNumber(window, ["last_tick"]);
  const windowTruncated = asBoolean(window.window_truncated) ?? false;
  const totalsScope = asString(window.totals_scope);
  const hasWindowMetadata = returnedTicks !== undefined || totalTicks !== undefined || totalTicksLowerBound !== undefined || totalsScope !== undefined;
  const byTick = new Map<number, { input: number; output: number; reasoning: number }>();
  const byModel = new Map<string, { input: number; output: number; reasoning: number }>();
  rows.forEach((row) => {
    const tick = byTick.get(row.tick) ?? { input: 0, output: 0, reasoning: 0 };
    tick.input += row.input; tick.output += row.output; tick.reasoning += row.reasoning; byTick.set(row.tick, tick);
    const model = byModel.get(row.model) ?? { input: 0, output: 0, reasoning: 0 };
    model.input += row.input; model.output += row.output; model.reasoning += row.reasoning; byModel.set(row.model, model);
  });
  const seriesSpec = [
    { name: "Input", key: "input" as const, color: chartTheme.blue },
    { name: "Output", key: "output" as const, color: chartTheme.gold },
    { name: "Reasoning", key: "reasoning" as const, color: chartTheme.olive },
  ];
  const option: EChartsOption = {
    color: seriesSpec.map((item) => item.color),
    grid: baseGrid,
    tooltip: baseTooltip,
    legend: { top: 0, textStyle: { color: chartTheme.muted } },
    xAxis: { ...baseAxis, type: "category", name: "tick", data: ticks },
    yAxis: { ...baseAxis, type: "value", name: "tokens", nameTextStyle: { color: chartTheme.muted } },
    series: seriesSpec.map((spec) => ({
      name: spec.name,
      type: "line",
      stack: "tokens",
      smooth: false,
      showSymbol: ticks.length < 20,
      symbolSize: 5,
      areaStyle: { opacity: .16 },
      lineStyle: { width: 2 },
      data: ticks.map((tick) => byTick.get(tick)?.[spec.key] ?? 0),
    })),
  };
  if (!rows.length) return <EmptyState title="Нет token usage" detail="Данные появятся после первого вызова модели в committed-тике." />;
  return (
    <div>
      {hasWindowMetadata ? (
        <div className={`token-window-scope${windowTruncated ? " truncated" : ""}`}>
          <strong>
            {windowTruncated ? "Последнее окно" : "Возвращённое окно"}: {formatInteger(returnedTicks ?? ticks.length)} ticks с token usage
            {windowTruncated && totalTicksLowerBound !== undefined ? <>; в запуске не менее {formatInteger(totalTicksLowerBound)}</> : null}
            {!windowTruncated && totalTicks !== undefined ? <> из {formatInteger(totalTicks)} total</> : null}
          </strong>
          <span>
            Серии и итоги ниже относятся к возвращённому окну{windowTruncated ? ", не ко всему запуску" : ""}.{totalsScope === "window" ? " Scope: window." : ""}
            {firstTick !== undefined || lastTick !== undefined ? <> Диапазон: tick {formatInteger(firstTick)}–{formatInteger(lastTick)}.</> : null}
          </span>
        </div>
      ) : null}
      <EChart
        option={option}
        height={310}
        ariaLabel={`Токены по ${windowTruncated ? `последним ${returnedTicks ?? ticks.length}; в запуске не менее ${totalTicksLowerBound ?? "неизвестного числа"}` : `возвращённым ${returnedTicks ?? ticks.length}${totalTicks !== undefined ? ` из ${totalTicks}` : ""}`} committed-тикам, input, output и reasoning`}
      />
      <div className="token-totals-heading">Итоги по отображённому окну</div>
      <div className="compact-table model-totals">
        <div className="compact-table-head"><span>Модель · window</span><span>Input</span><span>Output</span><span>Reasoning</span></div>
        {[...byModel.entries()].map(([model, totals]) => (
          <div className="compact-table-row" key={model}><strong>{model}</strong><code>{formatInteger(totals.input)}</code><code>{formatInteger(totals.output)}</code><code>{formatInteger(totals.reasoning)}</code></div>
        ))}
      </div>
    </div>
  );
});

interface SurvivalRow {
  generation: number;
  fact: string;
  basis: string;
  channel: string;
  rate: number;
  total: number;
}

function survivalRows(value: unknown): SurvivalRow[] {
  return unwrapArray(value, ["points", "curve", "knowledge_survival", "items", "data", "results"]).flatMap((item) => {
    const row = asRecord(item);
    const generation = firstNumber(row, ["generation"]);
    const fact = asString(row.fact_id) ?? asString(row.fact) ?? asString(row.knowledge_id);
    const rate = firstNumber(row, ["survival_rate", "rate", "value"]);
    if (generation === undefined || fact === undefined || rate === undefined) return [];
    return [{
      generation,
      fact,
      basis: asString(row.basis) ?? "authored",
      channel: asString(row.channel) ?? "written",
      rate,
      total: firstNumber(row, ["total_legacies", "total", "n"]) ?? 0,
    }];
  });
}

export const SurvivalCurves = memo(function SurvivalCurves({ data }: { data: unknown }): React.JSX.Element {
  const rows = useMemo(() => survivalRows(data), [data]);
  const dimensions = useMemo(() => {
    const bases: string[] = [];
    const channels: string[] = [];
    const basisSet = new Set<string>();
    const channelSet = new Set<string>();
    rows.forEach((row) => {
      if (!basisSet.has(row.basis)) {
        basisSet.add(row.basis);
        bases.push(row.basis);
      }
      if (!channelSet.has(row.channel)) {
        channelSet.add(row.channel);
        channels.push(row.channel);
      }
    });
    return { bases, channels, basisSet, channelSet };
  }, [rows]);
  const { bases, channels, basisSet, channelSet } = dimensions;
  const [basis, setBasis] = useState<string>(bases[0] ?? "authored");
  const [channel, setChannel] = useState<string>(channels[0] ?? "written");
  const activeBasis = basisSet.has(basis) ? basis : bases[0];
  const activeChannel = channelSet.has(channel) ? channel : channels[0];
  const indexed = useMemo(() => {
    const filtered: SurvivalRow[] = [];
    const generationSet = new Set<number>();
    const facts: string[] = [];
    const factSet = new Set<string>();
    const rateByFact = new Map<string, Map<number, number>>();
    let sampleTotal = 0;
    rows.forEach((row) => {
      if (row.basis !== activeBasis || row.channel !== activeChannel) return;
      filtered.push(row);
      generationSet.add(row.generation);
      sampleTotal += row.total;
      if (!factSet.has(row.fact)) {
        factSet.add(row.fact);
        facts.push(row.fact);
      }
      const rates = rateByFact.get(row.fact) ?? new Map<number, number>();
      // Array.find returned the first duplicate; do not overwrite it here.
      if (!rates.has(row.generation)) rates.set(row.generation, row.rate);
      rateByFact.set(row.fact, rates);
    });
    return {
      filtered,
      facts,
      generations: [...generationSet].sort((left, right) => left - right),
      rateByFact,
      sampleTotal,
    };
  }, [activeBasis, activeChannel, rows]);
  const { filtered, facts, generations, rateByFact, sampleTotal } = indexed;
  const option = useMemo<EChartsOption>(() => ({
    color: [...chartTheme.palette],
    grid: baseGrid,
    tooltip: { ...baseTooltip, valueFormatter: (value) => formatPercent(asNumber(value), 1) },
    legend: { type: "scroll", top: 0, textStyle: { color: chartTheme.muted } },
    xAxis: { ...baseAxis, type: "category", name: "generation", data: generations },
    yAxis: { ...baseAxis, type: "value", min: 0, max: 1, axisLabel: { ...baseAxis.axisLabel, formatter: (value: number) => `${Math.round(value * 100)}%` } },
    series: facts.map((fact, index) => {
      const rates = rateByFact.get(fact);
      return {
        name: fact,
        type: "line",
        showSymbol: true,
        symbol: index % 2 ? "emptyCircle" : "circle",
        lineStyle: { width: 2, type: index % 3 === 2 ? "dashed" : "solid" },
        data: generations.map((generation) => rates?.get(generation) ?? null),
      };
    }),
  }), [facts, generations, rateByFact]);
  if (!rows.length) return <EmptyState title="Кривая ещё не измерена" detail="Запустите measure во вкладке Audit; результат кешируется вне run-dir." />;
  return (
    <div>
      <div className="chart-controls">
        <label>Basis<select value={activeBasis ?? ""} onChange={(event) => setBasis(event.target.value)}>{bases.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label>Channel<select value={activeChannel ?? ""} onChange={(event) => setChannel(event.target.value)}>{channels.map((item) => <option key={item}>{item}</option>)}</select></label>
        <span>n = {formatInteger(sampleTotal)}</span>
      </div>
      {generations.length < 3 ? (
        <div className="sparse-values"><p>Недостаточно поколений для честной линии.</p>{filtered.map((row) => <span key={`${row.fact}-${row.generation}`}><b>{row.fact} · G{row.generation}</b><code>{formatPercent(row.rate, 1)} (n={row.total})</code></span>)}</div>
      ) : <EChart option={option} height={340} ariaLabel={`Выживание знаний по поколениям, ${activeBasis ?? "unknown basis"}, ${activeChannel ?? "unknown channel"}`} />}
    </div>
  );
});

interface BehaviorRow {
  generation: number;
  risky: number;
  deep: number;
  poison: number;
  collapse: number;
  agents: number;
}

function behaviorRows(value: unknown): BehaviorRow[] {
  return unwrapArray(value, ["points", "curve", "behavior", "items", "data", "results"]).flatMap((item) => {
    const row = asRecord(item);
    const generation = firstNumber(row, ["generation"]);
    if (generation === undefined) return [];
    return [{
      generation,
      risky: firstNumber(row, ["risky_eat_rate", "risky_berry_rate"]) ?? 0,
      deep: firstNumber(row, ["deep_dig_rate"]) ?? 0,
      poison: firstNumber(row, ["poison_damage_events", "poison_events"]) ?? 0,
      collapse: firstNumber(row, ["collapse_events"]) ?? 0,
      agents: firstNumber(row, ["agents", "agent_count"]) ?? 0,
    }];
  }).sort((left, right) => left.generation - right.generation);
}

export const BehaviorCurves = memo(function BehaviorCurves({ data }: { data: unknown }): React.JSX.Element {
  const rows = behaviorRows(data);
  if (!rows.length) return <EmptyState title="Поведенческие метрики отсутствуют" detail="Measure рассчитает рискованные действия по поколениям." />;
  if (rows.length < 3) return (
    <div className="sparse-values"><p>Для тренда нужны как минимум три поколения.</p>{rows.map((row) => <span key={row.generation}><b>Generation {row.generation}</b><code>risky {formatPercent(row.risky, 1)} · deep {formatPercent(row.deep, 1)}</code></span>)}</div>
  );
  const option: EChartsOption = {
    color: [chartTheme.orange, chartTheme.gold, chartTheme.pink, chartTheme.olive],
    grid: baseGrid,
    tooltip: baseTooltip,
    legend: { top: 0, textStyle: { color: chartTheme.muted } },
    xAxis: { ...baseAxis, type: "category", name: "generation", data: rows.map((row) => row.generation) },
    yAxis: [
      { ...baseAxis, type: "value", min: 0, max: 1, name: "rate", axisLabel: { ...baseAxis.axisLabel, formatter: (value: number) => `${Math.round(value * 100)}%` } },
      { ...baseAxis, type: "value", min: 0, name: "events", splitLine: { show: false } },
    ],
    series: [
      { name: "Risky berry eats", type: "line", symbol: "circle", data: rows.map((row) => row.risky) },
      { name: "Deep digs", type: "line", symbol: "emptyCircle", lineStyle: { type: "dashed" }, data: rows.map((row) => row.deep) },
      { name: "Poison events", type: "bar", yAxisIndex: 1, barMaxWidth: 18, data: rows.map((row) => row.poison), itemStyle: { opacity: .48 } },
      { name: "Collapse events", type: "bar", yAxisIndex: 1, barMaxWidth: 18, data: rows.map((row) => row.collapse), itemStyle: { opacity: .42 } },
    ],
  };
  return (
    <div>
      <EChart option={option} height={340} ariaLabel="Рискованные действия и последствия по поколениям" />
      <p className="chart-note">Rate рассчитан внутри поколения; столбцы показывают наблюдённые события вреда. Agents: {rows.map((row) => `G${row.generation}=${row.agents}`).join(", ")}.</p>
    </div>
  );
});

export function MetricRawPreview({ name, data }: { name: string; data: unknown }): React.JSX.Element {
  const rows = unwrapArray(data);
  if (!rows.length) return <EmptyState title={`${humanize(name)}: no data`} />;
  return <span>{rows.length} records</span>;
}
