import { useCallback, useEffect, useRef, useState } from "react";
import { api, subscribeRunEvents } from "../api/client";
import { asBoolean, asNumber, asRecord, asString, normalizeAgents, normalizeTicks, normalizeWorld, unwrapArray } from "../api/normalize";
import type { AgentRecord, RunSummary, TerrariumEvent, TickRecord, WorldSnapshot } from "../api/types";
import { ErrorNotice, LoadingBlock, MetricCard, Panel, SanitizedText, StatusChip } from "../components/Primitives";
import { AuditPanel, waitForJob } from "../components/run/AuditPanel";
import { EventStream } from "../components/run/EventStream";
import { BehaviorCurves, BudgetGauges, SurvivalCurves, TokenChart } from "../components/run/MetricCharts";
import { TickProgress } from "../components/run/TickProgress";
import { AgentGantt, LegacyDag, LineageTree } from "../components/run/TopologyCharts";
import { WorldMap } from "../components/run/WorldMap";
import { formatInteger, shortId, timestampLabel } from "../lib/format";
import { useAsync, useDebouncedCallback, useDocumentTitle } from "../lib/hooks";
import { Link } from "../router";

type TabId = "live" | "analysis" | "topology" | "world" | "events" | "audit";
const TOPOLOGY_PAGE_LIMIT = 2_000;

interface CoreTelemetry {
  ticks: TickRecord[];
  totalTicks?: number;
  tickWindowTruncated: boolean;
  tokens: unknown;
  budget: unknown;
}

interface PageWindow {
  offset: number;
  limit?: number;
  total?: number;
  returned: number;
  hasMore: boolean;
}

interface TopologyTelemetry {
  agents: AgentRecord[];
  lineage: unknown;
  legacies: unknown;
  pages: {
    agents: PageWindow;
    lineage: PageWindow;
    legacies: PageWindow;
  };
}

interface AnalysisTelemetry {
  survival: unknown;
  behavior: unknown;
}

async function loadCoreTelemetry(runName: string): Promise<CoreTelemetry> {
  const [tickResult, tokens, budget] = await Promise.all([
    api.runData(runName, "ticks"),
    api.runData(runName, "metrics/tokens"),
    api.runData(runName, "metrics/budget"),
  ]);
  const tickEnvelope = asRecord(tickResult);
  return {
    ticks: normalizeTicks(tickResult),
    totalTicks: asNumber(tickEnvelope.total_ticks),
    tickWindowTruncated: asBoolean(tickEnvelope.window_truncated) ?? false,
    tokens,
    budget,
  };
}

function pageWindow(value: unknown, returned: number): PageWindow {
  const page = asRecord(asRecord(value).page);
  const total = asNumber(page.total);
  const offset = asNumber(page.offset) ?? 0;
  return {
    offset,
    limit: asNumber(page.limit),
    total,
    returned,
    hasMore: asBoolean(page.has_more) ?? (total !== undefined && offset + returned < total),
  };
}

async function loadTopology(runName: string, offset: number): Promise<TopologyTelemetry> {
  const query = new URLSearchParams({ offset: String(offset), limit: String(TOPOLOGY_PAGE_LIMIT) });
  const [agents, lineage, legacies] = await Promise.all([
    api.runData(runName, "agents", query),
    api.runData(runName, "lineage", query),
    api.runData(runName, "legacies", query),
  ]);
  const normalizedAgents = normalizeAgents(agents);
  return {
    agents: normalizedAgents,
    lineage,
    legacies,
    pages: {
      agents: pageWindow(agents, normalizedAgents.length),
      lineage: pageWindow(lineage, unwrapArray(lineage, ["nodes", "items", "data", "results"]).length),
      legacies: pageWindow(legacies, unwrapArray(legacies, ["legacies", "items", "data", "results"]).length),
    },
  };
}

function TopologyWindowSummary({ pages, loading, onPrevious, onNext }: {
  pages: TopologyTelemetry["pages"];
  loading: boolean;
  onPrevious: () => void;
  onNext: () => void;
}): React.JSX.Element {
  const entries: Array<[string, PageWindow]> = [
    ["Agents", pages.agents],
    ["Lineage nodes", pages.lineage],
    ["Legacies", pages.legacies],
  ];
  const offset = pages.agents.offset;
  const limit = pages.agents.limit ?? TOPOLOGY_PAGE_LIMIT;
  const hasPrevious = entries.some(([, page]) => page.offset > 0);
  const hasNext = entries.some(([, page]) => page.hasMore);
  const paged = entries.some(([, page]) => page.offset > 0 || (page.total !== undefined && page.total > page.returned));
  const pageReturned = Math.max(...entries.map(([, page]) => page.returned), 0);
  return (
    <div className={`topology-window-summary${paged ? " truncated" : ""}`}>
      <div className="topology-window-heading">
        <strong>{paged ? "Показана ограниченная серверная страница" : "Server page coverage"}</strong>
        <code>shared offset {formatInteger(offset)} · limit {formatInteger(limit)}</code>
      </div>
      <div className="topology-window-counts">
        {entries.map(([label, page]) => (
          <span key={label}>
            {label}: <b>{formatInteger(page.returned)}</b>{page.total !== undefined ? <> / total {formatInteger(page.total)}</> : " returned"}
            {page.hasMore ? " · есть следующая страница" : ""}
          </span>
        ))}
      </div>
      {paged ? <small>Остальные страницы не загружаются автоматически, чтобы не блокировать GUI на длинных запусках.</small> : null}
      <div className="topology-server-pager" role="group" aria-label="Навигация по серверным страницам topology">
        <button type="button" className="button button-small button-ghost" disabled={loading || !hasPrevious} onClick={onPrevious}>← Предыдущие {formatInteger(limit)}</button>
        <span>{pageReturned ? <>records {formatInteger(offset + 1)}–{formatInteger(offset + pageReturned)}</> : <>нет records с offset {formatInteger(offset)}</>}</span>
        <button type="button" className="button button-small button-ghost" disabled={loading || !hasNext} onClick={onNext}>Следующие {formatInteger(limit)} →</button>
      </div>
    </div>
  );
}

async function loadAnalysis(runName: string): Promise<AnalysisTelemetry> {
  const [survival, behavior] = await Promise.all([
    api.runData(runName, "metrics/knowledge-survival"),
    api.runData(runName, "metrics/behavior"),
  ]);
  return { survival, behavior };
}

function analyticsPending(value: unknown): boolean {
  return asRecord(value).status === "pending";
}

function analyticsFailed(value: unknown): boolean {
  const status = asString(asRecord(value).status)?.toLowerCase();
  return status === "failed" || status === "error";
}

function AnalysisFailure({ value, retrying, onRetry }: {
  value: unknown;
  retrying: boolean;
  onRetry: () => void;
}): React.JSX.Element {
  const record = asRecord(value);
  return (
    <div className="analysis-failure" role="alert">
      <div>
        <strong>Measure завершился с ошибкой</strong>
        <SanitizedText value={asString(record.error) ?? "Measurement data could not be produced."} />
        {record.job_id !== undefined ? <small>job <SanitizedText value={record.job_id} limit={256} /></small> : null}
      </div>
      <button type="button" className="button button-small button-danger" disabled={retrying} onClick={onRetry}>
        {retrying ? "Measure выполняется…" : "Повторить measure"}
      </button>
    </div>
  );
}

function mergeEvents(current: TerrariumEvent[], incoming: TerrariumEvent[], max = 10_000): TerrariumEvent[] {
  const bySeq = new Map(current.map((event) => [event.seq, event]));
  incoming.forEach((event) => bySeq.set(event.seq, event));
  return [...bySeq.values()].sort((left, right) => left.seq - right.seq).slice(-max);
}

function tabFromUrl(): TabId {
  const value = new URLSearchParams(window.location.search).get("tab");
  return value === "analysis" || value === "topology" || value === "world" || value === "events" || value === "audit" ? value : "live";
}

function RunHeader({ run, busy, onStop, onResume }: {
  run: RunSummary;
  busy?: string;
  onStop: () => void;
  onResume: () => void;
}): React.JSX.Element {
  return (
    <>
      <div className="breadcrumbs"><Link href="/">Запуски</Link><span>/</span><span>{run.name}</span></div>
      <header className="run-header">
        <div>
          <div className="run-title-row"><h1>{run.name}</h1><StatusChip status={run.status} /></div>
          <div className="run-metadata">
            <span>run_id <code>{run.run_id ?? "—"}</code></span>
            <span>config <code>{run.config_name ?? "snapshot"}</code></span>
            <span>started <code>{timestampLabel(run.started_at)}</code></span>
            {run.pid ? <span>pid <code>{run.pid}</code></span> : null}
          </div>
        </div>
        <div className="run-controls">
          {run.status === "running" || run.status === "initializing" || run.status === "stopping" ? (
            <button type="button" className="button button-danger" disabled={Boolean(busy) || run.status === "stopping"} onClick={onStop}>{busy === "stop" || run.status === "stopping" ? "Останавливаем…" : "Остановить (SIGINT)"}</button>
          ) : null}
          {run.status === "resumable" ? (
            <button type="button" className="button button-primary" disabled={Boolean(busy)} onClick={onResume}>{busy === "resume" ? "Продолжаем…" : "Продолжить"}</button>
          ) : null}
        </div>
      </header>
    </>
  );
}

export function RunPage({ runName }: { runName: string }): React.JSX.Element {
  useDocumentTitle(`Terrarium · ${runName}`);
  const [tab, setTab] = useState<TabId>(tabFromUrl);
  const [version, setVersion] = useState(0);
  const [events, setEvents] = useState<TerrariumEvent[]>([]);
  const [streamState, setStreamState] = useState<"connecting" | "open" | "closed" | "error">("connecting");
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [controlBusy, setControlBusy] = useState<string>();
  const [controlError, setControlError] = useState<Error>();
  const [ticksPerMinute, setTicksPerMinute] = useState<number>();
  const [worldTick, setWorldTick] = useState<number>();
  const [followLive, setFollowLive] = useState(true);
  const [analysisVersion, setAnalysisVersion] = useState(0);
  const [analysisRetrying, setAnalysisRetrying] = useState(false);
  const [analysisRetryError, setAnalysisRetryError] = useState<Error>();
  const [topologyVersion, setTopologyVersion] = useState(0);
  const [topologyOffset, setTopologyOffset] = useState(0);
  const [worldVersion, setWorldVersion] = useState(0);
  const sequenceRef = useRef(-1);
  const observationsRef = useRef<Array<{ tick: number; time: number }>>([]);

  useEffect(() => setTopologyOffset(0), [runName]);

  const runState = useAsync(() => api.run(runName), [runName, version]);
  const telemetry = useAsync(
    () => loadCoreTelemetry(runName),
    [runName, version, runState.data?.status],
  );
  const topology = useAsync(
    () => tab === "topology"
      ? loadTopology(runName, topologyOffset)
      : Promise.resolve({
        agents: [],
        lineage: undefined,
        legacies: undefined,
        pages: {
          agents: pageWindow(undefined, 0),
          lineage: pageWindow(undefined, 0),
          legacies: pageWindow(undefined, 0),
        },
      }),
    [runName, tab, topologyOffset, topologyVersion, runState.data?.status],
  );
  const analysis = useAsync(
    () => tab === "analysis" ? loadAnalysis(runName) : Promise.resolve({ survival: undefined, behavior: undefined }),
    [runName, tab, analysisVersion, runState.data?.status],
  );
  const refreshDebounced = useDebouncedCallback(() => setVersion((value) => value + 1), 500);

  useEffect(() => {
    const status = runState.data?.status;
    if (status !== "running" && status !== "external" && status !== "initializing" && status !== "stopping") return;
    const timer = window.setInterval(() => void runState.refresh(), 3_000);
    return () => window.clearInterval(timer);
  }, [runState.data?.status, runState.refresh]);

  useEffect(() => {
    const status = runState.data?.status;
    const active = status === "running" || status === "external" || status === "initializing" || status === "stopping";
    if (!active || !telemetry.error) return;
    const timer = window.setTimeout(() => void telemetry.refresh(), 2_000);
    return () => window.clearTimeout(timer);
  }, [runState.data?.status, telemetry.error, telemetry.refresh]);

  const backfill = useCallback(async (afterSeq: number, latestSeq?: number) => {
    try {
      let cursor = Math.max(-1, afterSeq);
      let page = 0;
      while (true) {
        const incoming = await api.events(runName, cursor, 500);
        if (!incoming.length) break;
        setEvents((current) => mergeEvents(current, incoming));
        const highest = incoming.at(-1)?.seq;
        if (highest === undefined || highest <= cursor) break;
        cursor = highest;
        sequenceRef.current = Math.max(sequenceRef.current, highest);
        if ((latestSeq !== undefined && cursor >= latestSeq) || incoming.length < 500) break;
        page += 1;
        if (page % 5 === 0) await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      }
    } catch {
      // EventSource will reconnect; the visible connection state communicates transport failure.
    }
  }, [runName]);

  useEffect(() => {
    let cancelled = false;
    setEvents([]);
    sequenceRef.current = -1;
    setStreamState("connecting");
    let unsubscribe: () => void = () => {};
    void api.events(runName, -1, 500, true).then((initial) => {
      if (cancelled) return;
      setEvents(initial);
      sequenceRef.current = initial.at(-1)?.seq ?? -1;
      unsubscribe = subscribeRunEvents(runName, {
        onEvent: (event) => {
          sequenceRef.current = Math.max(sequenceRef.current, event.seq);
          setEvents((current) => mergeEvents(current, [event]));
          if (event.type === "tick_commit" || event.type === "tick") refreshDebounced();
        },
        onGap: (afterSeq, latestSeq) => void backfill(
          Math.max(sequenceRef.current, Number.isFinite(afterSeq) ? Number(afterSeq) : -1),
          Number.isFinite(latestSeq) ? Number(latestSeq) : undefined,
        ),
        onState: setStreamState,
      }, sequenceRef.current);
    }).catch(() => {
      if (!cancelled) {
        unsubscribe = subscribeRunEvents(runName, {
          onEvent: (event) => {
            sequenceRef.current = Math.max(sequenceRef.current, event.seq);
            setEvents((current) => mergeEvents(current, [event]));
            refreshDebounced();
          },
          onGap: () => void backfill(sequenceRef.current),
          onState: setStreamState,
        });
      }
    });
    return () => { cancelled = true; unsubscribe(); };
  }, [backfill, refreshDebounced, runName]);

  const currentTick = runState.data?.current_tick ?? telemetry.data?.ticks.at(-1)?.tick;
  useEffect(() => {
    if (currentTick === undefined) return;
    const now = performance.now();
    const observations = observationsRef.current;
    if (observations.at(-1)?.tick !== currentTick) observations.push({ tick: currentTick, time: now });
    while (observations.length > 12) observations.shift();
    const first = observations[0];
    const last = observations.at(-1);
    if (first && last && last.tick > first.tick && last.time > first.time) {
      setTicksPerMinute((last.tick - first.tick) / ((last.time - first.time) / 60_000));
    }
    if (followLive) setWorldTick(currentTick);
  }, [currentTick, followLive]);

  useEffect(() => {
    if (tab !== "analysis" || (!analyticsPending(analysis.data?.survival) && !analyticsPending(analysis.data?.behavior))) return;
    const timer = window.setTimeout(() => setAnalysisVersion((value) => value + 1), 1_000);
    return () => window.clearTimeout(timer);
  }, [analysis.data, tab]);

  const worldState = useAsync(async (): Promise<WorldSnapshot | undefined> => {
    if (tab !== "world") return undefined;
    const query = new URLSearchParams();
    if (worldTick !== undefined) query.set("tick", String(worldTick));
    return normalizeWorld(await api.runData(runName, "world", query));
  }, [runName, tab, worldTick, worldVersion, runState.data?.status]);

  const setActiveTab = (next: TabId) => {
    setTab(next);
    const url = new URL(window.location.href);
    if (next === "live") url.searchParams.delete("tab");
    else url.searchParams.set("tab", next);
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  };

  const control = async (action: "stop" | "resume") => {
    setControlBusy(action);
    setControlError(undefined);
    try {
      if (action === "stop") await api.stopRun(runName);
      else await api.resumeRun(runName);
      window.setTimeout(() => setVersion((value) => value + 1), 300);
    } catch (reason) {
      setControlError(reason instanceof Error ? reason : new Error(String(reason)));
    } finally {
      setControlBusy(undefined);
    }
  };

  const retryAnalysis = async () => {
    setAnalysisRetrying(true);
    setAnalysisRetryError(undefined);
    let started = false;
    try {
      const initial = await api.tool(runName, "measure");
      started = true;
      const result = await waitForJob(initial);
      const status = result.status?.toLowerCase();
      const resultRecord = asRecord(result.result);
      if (status !== "completed" && status !== "succeeded") {
        throw new Error(
          result.error
          ?? asString(resultRecord.error)
          ?? `Measure job ${result.job_id ?? ""} завершился со статусом ${result.status ?? "unknown"}`.trim(),
        );
      }
    } catch (reason) {
      setAnalysisRetryError(reason instanceof Error ? reason : new Error(String(reason)));
    } finally {
      if (started) setAnalysisVersion((value) => value + 1);
      setAnalysisRetrying(false);
    }
  };

  const loadOlder = async () => {
    const first = events[0]?.seq;
    if (first === undefined || first <= 0) return;
    setLoadingOlder(true);
    try {
      const incoming = await api.eventsBefore(runName, first, 500);
      setEvents((current) => mergeEvents(current, incoming));
    } finally { setLoadingOlder(false); }
  };

  const tabs: Array<{ id: TabId; label: string; count?: number }> = [
    { id: "live", label: "Live" },
    { id: "analysis", label: "Анализ" },
    { id: "topology", label: "Топология" },
    { id: "world", label: "Мир" },
    { id: "events", label: "События", count: events.length },
    { id: "audit", label: "Audit" },
  ];

  if (runState.loading && !runState.data) return <div className="page"><LoadingBlock label="Открываем эксперимент" /></div>;
  if (runState.error) return <div className="page"><ErrorNotice error={runState.error} retry={() => void runState.refresh()} /></div>;
  const run = runState.data;
  if (!run) return <div className="page"><LoadingBlock /></div>;
  const data = telemetry.data;

  return (
    <div className="page run-page">
      <RunHeader run={run} busy={controlBusy} onStop={() => void control("stop")} onResume={() => void control("resume")} />
      {controlError ? <ErrorNotice error={controlError} /> : null}
      <nav className="tab-bar" aria-label="Данные запуска">
        {tabs.map((item) => <button type="button" key={item.id} className={tab === item.id ? "active" : undefined} aria-selected={tab === item.id} onClick={() => setActiveTab(item.id)}>{item.label}{item.count !== undefined ? <span>{formatInteger(item.count)}</span> : null}</button>)}
      </nav>

      {telemetry.loading && !data ? <LoadingBlock label="Читаем read-only проекцию" /> : null}
      {telemetry.error ? <ErrorNotice error={telemetry.error} retry={() => void telemetry.refresh()} /> : null}

      {tab === "live" && data ? (
        <div className="tab-content live-tab">
          <Panel title="Прогресс тиков" subtitle="Только fsync-подтверждённые tick_commit; скорость — локальное наблюдение GUI" actions={<span className={`stream-state stream-${streamState}`}><i />{streamState}</span>}>
            <TickProgress run={run} ticks={data.ticks} ticksPerMinute={ticksPerMinute} />
          </Panel>
          <div className="metric-grid run-summary-metrics">
            <MetricCard label="Generation" value={run.generation !== undefined ? `G${run.generation}` : "—"} detail={run.target_generations !== undefined ? `target G${run.target_generations}` : "target from snapshot"} tone="blue" />
            <MetricCard
              label="Committed ticks"
              value={formatInteger(data.totalTicks ?? data.ticks.length)}
              detail={data.tickWindowTruncated ? `latest ${formatInteger(data.ticks.length)} loaded · last ${formatInteger(currentTick)}` : `last ${formatInteger(currentTick)}`}
            />
            <MetricCard label="Events in buffer" value={formatInteger(events.length)} detail={`seq ${formatInteger(sequenceRef.current)}`} />
            <MetricCard label="Run fingerprint" value={shortId(asRecord(run.raw).config_sha256 as string | undefined, 12)} detail="snapshot config SHA-256" />
          </div>
          <div className="two-column-grid">
            <Panel title="Бюджет" subtitle="Использование относительно immutable config limits"><BudgetGauges data={data.budget} /></Panel>
            <Panel title="Токены" subtitle="Input, output и reasoning только в явно обозначенном server window"><TokenChart data={data.tokens} /></Panel>
          </div>
          <Panel title="Последние события" subtitle="Санитизированная проекция; raw_text не запрашивается" actions={<button type="button" className="button button-small button-ghost" onClick={() => setActiveTab("events")}>Открыть поток →</button>}>
            <EventStream events={events.slice(-40)} connection={streamState} />
          </Panel>
        </div>
      ) : null}

      {tab === "analysis" && data ? (
        <div className="tab-content two-column-grid analysis-grid">
          {analysis.error ? <div className="analysis-span"><ErrorNotice error={analysis.error} retry={() => void analysis.refresh()} /></div> : null}
          {analysisRetryError ? <div className="analysis-span"><ErrorNotice error={analysisRetryError} /></div> : null}
          <Panel
            title="Выживание знаний"
            subtitle="Entailment rate по поколениям; basis и канал выбираются явно"
            actions={<button type="button" className="button button-small button-ghost" disabled={analysis.loading} onClick={() => setAnalysisVersion((value) => value + 1)}>Обновить</button>}
          >
            {analysis.loading ? <LoadingBlock label="Проверяем кеш measure" />
              : analyticsPending(analysis.data?.survival) ? <div className="loading-block"><span className="spinner" /><span>CLI measure выполняется; повтор через 1 секунду</span></div>
                : analyticsFailed(analysis.data?.survival) ? <AnalysisFailure value={analysis.data?.survival} retrying={analysisRetrying} onRetry={() => void retryAnalysis()} />
                  : <SurvivalCurves data={analysis.data?.survival} />}
          </Panel>
          <Panel title="Адаптация поведения" subtitle="Рискованные действия и фактические последствия">
            {analysis.loading ? <LoadingBlock label="Проверяем кеш measure" />
              : analyticsPending(analysis.data?.behavior) ? <div className="loading-block"><span className="spinner" /><span>Ожидаем проверенные measurement points</span></div>
                : analyticsFailed(analysis.data?.behavior) ? <AnalysisFailure value={analysis.data?.behavior} retrying={analysisRetrying} onRetry={() => void retryAnalysis()} />
                  : <BehaviorCurves data={analysis.data?.behavior} />}
          </Panel>
        </div>
      ) : null}

      {tab === "topology" ? (
        topology.loading ? <LoadingBlock label="Читаем topology по запросу" />
          : topology.error ? <ErrorNotice error={topology.error} retry={() => void topology.refresh()} />
            : (
              <div className="tab-content topology-grid">
                {topology.data ? (
                  <TopologyWindowSummary
                    pages={topology.data.pages}
                    loading={topology.loading}
                    onPrevious={() => setTopologyOffset((value) => Math.max(0, value - TOPOLOGY_PAGE_LIMIT))}
                    onNext={() => setTopologyOffset((value) => value + TOPOLOGY_PAGE_LIMIT)}
                  />
                ) : null}
                <Panel
                  title="Дерево линий"
                  subtitle="Поколения слева направо; доступен pan/zoom и раскрытие ветвей"
                  actions={<button type="button" className="button button-small button-ghost" onClick={() => setTopologyVersion((value) => value + 1)}>Обновить topology</button>}
                ><LineageTree data={topology.data?.lineage} /></Panel>
                <Panel title="Legacy DAG" subtitle="Рёбра направлены от родительского наследия к производному"><LegacyDag data={topology.data?.legacies} /></Panel>
                <Panel title="Жизненный таймлайн агентов" subtitle="Полосы: born_tick → died_tick или последний committed tick"><AgentGantt agents={topology.data?.agents ?? []} currentTick={currentTick} page={topology.data?.pages.agents} /></Panel>
              </div>
            )
      ) : null}

      {tab === "world" ? (
        <div className="tab-content">
          <Panel
            title="Карта мира"
            subtitle="Схематическая SVG-проекция committed checkpoint; незакоммиченный state не показывается"
            actions={(
              <div className="world-controls">
                <label><input type="checkbox" checked={followLive} onChange={(event) => setFollowLive(event.target.checked)} /> следовать за live</label>
                <input type="range" min={0} max={Math.max(1, currentTick ?? 1)} value={worldTick ?? currentTick ?? 0} disabled={followLive} onChange={(event) => { setFollowLive(false); setWorldTick(Number(event.target.value)); }} aria-label="Tick карты" />
                <code>t{formatInteger(worldTick)}</code>
                <button type="button" className="button button-small button-ghost" disabled={worldState.loading} onClick={() => setWorldVersion((value) => value + 1)}>Обновить</button>
              </div>
            )}
          >
            {worldState.loading && !worldState.data ? <LoadingBlock label="Читаем checkpoint" /> : null}
            {worldState.error ? <ErrorNotice error={worldState.error} retry={() => void worldState.refresh()} /> : null}
            {worldState.data ? <WorldMap world={worldState.data} /> : null}
          </Panel>
        </div>
      ) : null}

      {tab === "events" ? (
        <div className="tab-content"><Panel title="Поток событий" subtitle="SSE возобновляется по id:seq; gap заполняется через REST"><EventStream events={events} connection={streamState} onLoadOlder={events[0]?.seq ? () => void loadOlder() : undefined} loadingOlder={loadingOlder} /></Panel></div>
      ) : null}

      {tab === "audit" ? (
        <div className="tab-content"><Panel title="Audit & инструменты" subtitle="CLI-команды выполняются отдельным subprocess; активный writer-lock даёт 409"><AuditPanel runName={runName} status={run.status} onToolCompleted={(tool) => {
          if (tool === "measure") {
            setAnalysisVersion((value) => value + 1);
          }
          if (tool === "rebuild") {
            setVersion((value) => value + 1);
            setTopologyVersion((value) => value + 1);
            setWorldVersion((value) => value + 1);
          }
        }} /></Panel></div>
      ) : null}
    </div>
  );
}
