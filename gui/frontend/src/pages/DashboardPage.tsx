import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { ConfigSummary, RunSummary } from "../api/types";
import { ErrorNotice, LoadingBlock, MetricCard, Panel, StatusChip } from "../components/Primitives";
import { formatInteger, timestampLabel } from "../lib/format";
import { useAsync, useDocumentTitle } from "../lib/hooks";
import { Link, navigate } from "../router";

function RunRow({ run }: { run: RunSummary }): React.JSX.Element {
  return (
    <Link className="run-row" href={`/runs/${encodeURIComponent(run.name)}`}>
      <span className="run-ident">
        <span className="run-glyph" aria-hidden="true">⌬</span>
        <span><strong>{run.name}</strong><small>{run.config_name ?? run.run_id ?? "config unknown"}</small></span>
      </span>
      <span className="mono tabular">{formatInteger(run.current_tick)}</span>
      <span className="mono tabular">{run.generation !== undefined ? `G${run.generation}` : "—"}</span>
      <span><StatusChip status={run.status} /></span>
      <span className="row-arrow" aria-hidden="true">→</span>
    </Link>
  );
}

export function DashboardPage(): React.JSX.Element {
  useDocumentTitle("Terrarium · Обзор");
  const runs = useAsync(() => api.runs(), []);
  const configs = useAsync(() => api.configs(), []);
  const [configName, setConfigName] = useState("");
  const [runName, setRunName] = useState("");
  const [maxTicks, setMaxTicks] = useState("");
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<Error>();

  useEffect(() => {
    if (!configName && configs.data?.[0]) setConfigName(configs.data[0].name);
  }, [configName, configs.data]);

  useEffect(() => {
    const timer = window.setInterval(() => void runs.refresh(), 4_000);
    return () => window.clearInterval(timer);
  }, [runs.refresh]);

  const summary = useMemo(() => {
    const values = runs.data ?? [];
    return {
      running: values.filter((run) => run.status === "running" || run.status === "external" || run.status === "initializing" || run.status === "stopping").length,
      resumable: values.filter((run) => run.status === "resumable").length,
      completed: values.filter((run) => run.status === "completed").length,
      latestSeq: Math.max(0, ...values.map((run) => run.last_seq ?? 0)),
    };
  }, [runs.data]);

  const start = async (event: FormEvent) => {
    event.preventDefault();
    if (!configName || !runName.trim()) return;
    setStarting(true);
    setStartError(undefined);
    try {
      await api.startRun({
        config_name: configName,
        run_name: runName.trim(),
        max_ticks: maxTicks ? Number(maxTicks) : undefined,
      });
      navigate(`/runs/${encodeURIComponent(runName.trim())}`);
    } catch (reason) {
      setStartError(reason instanceof Error ? reason : new Error(String(reason)));
    } finally {
      setStarting(false);
    }
  };

  return (
    <div className="page dashboard-page">
      <section className="hero">
        <div>
          <span className="eyebrow">Deterministic cultural evolution lab</span>
          <h1>Эксперименты под наблюдением</h1>
          <p>Управляйте запусками, следите только за durable-тиками и исследуйте передачу знаний между поколениями.</p>
        </div>
        <div className="hero-seal" aria-label="Аудируемая среда">
          <span>sealed</span>
          <strong>SHA·256</strong>
          <small>audit surface</small>
        </div>
      </section>

      <div className="metric-grid dashboard-metrics">
        <MetricCard label="Активные процессы" value={formatInteger(summary.running)} detail="writer lock занят" tone="blue" />
        <MetricCard label="Можно продолжить" value={formatInteger(summary.resumable)} detail="checkpoint доступен" tone="gold" />
        <MetricCard label="Завершено" value={formatInteger(summary.completed)} detail="все линии достигли цели" />
        <MetricCard label="Последний seq" value={formatInteger(summary.latestSeq)} detail="по всем найденным запускам" />
      </div>

      <div className="dashboard-grid">
        <Panel
          title="Запуски"
          subtitle="Статус выводится по writer-lock и последнему checkpoint"
          className="runs-panel"
          actions={<button type="button" className="button button-small button-ghost" onClick={() => void runs.refresh()}>Обновить</button>}
        >
          <div className="run-table-head"><span>Эксперимент</span><span>Committed tick</span><span>Gen</span><span>Статус</span><span /></div>
          {runs.loading && !runs.data ? <LoadingBlock /> : null}
          {runs.error ? <ErrorNotice error={runs.error} retry={() => void runs.refresh()} /> : null}
          {runs.data?.length ? runs.data.map((run) => <RunRow key={run.name} run={run} />) : null}
          {!runs.loading && !runs.error && !runs.data?.length ? (
            <div className="empty-state"><div className="empty-mark">∅</div><strong>Запусков пока нет</strong><p>Создайте первый эксперимент в форме справа.</p></div>
          ) : null}
        </Panel>

        <Panel title="Новый запуск" subtitle="Конфигурация будет снапшотирована до старта" className="new-run-panel">
          <form className="stack-form" onSubmit={(event) => void start(event)}>
            <label className="field">
              <span>Конфигурация</span>
              <select value={configName} onChange={(event) => setConfigName(event.target.value)} required>
                {!configs.data?.length ? <option value="">Нет конфигураций</option> : null}
                {configs.data?.map((config: ConfigSummary) => <option value={config.name} key={config.name}>{config.name}</option>)}
              </select>
            </label>
            <label className="field">
              <span>Имя директории запуска</span>
              <input value={runName} onChange={(event) => setRunName(event.target.value)} placeholder="pilot-2026-07-19" pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,95}" required />
              <small>Только буквы, цифры, _ и -. Путь выбирает сервер.</small>
            </label>
            <label className="field">
              <span>Дополнительные committed ticks <i>optional</i></span>
              <input type="number" min="0" step="1" value={maxTicks} onChange={(event) => setMaxTicks(event.target.value)} placeholder="без CLI-ограничения" />
              <small>Бюджет только для этого invocation; это не абсолютный номер tick и не цель всего эксперимента.</small>
            </label>
            {startError ? <ErrorNotice error={startError} /> : null}
            <button type="submit" className="button button-primary button-wide" disabled={starting || !configs.data?.length}>
              {starting ? "Запускаем…" : "Запустить эксперимент"}
            </button>
            <div className="form-footnote"><span aria-hidden="true">i</span><p>SIGINT завершает текущий процесс. Продолжение возможно только с байт-в-байт тем же снапшотом конфигурации.</p></div>
          </form>
        </Panel>
      </div>

      {runs.data?.[0]?.started_at ? (
        <p className="page-caption">Последний известный старт: {timestampLabel(runs.data[0].started_at)}</p>
      ) : null}
    </div>
  );
}
