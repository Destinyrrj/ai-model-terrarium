import type { RunSummary, TickRecord } from "../../api/types";
import { formatInteger, formatNumber } from "../../lib/format";

export function TickProgress({ run, ticks, ticksPerMinute }: {
  run: RunSummary;
  ticks: TickRecord[];
  ticksPerMinute?: number;
}): React.JSX.Element {
  const lastTick = run.current_tick ?? ticks.at(-1)?.tick ?? 0;
  const invocationStart = run.invocation_start_tick;
  const invocationTarget = run.invocation_target_tick;
  const invocationRange = invocationStart !== undefined
    && invocationTarget !== undefined
    && invocationTarget >= invocationStart
    ? { start: invocationStart, target: invocationTarget, budget: invocationTarget - invocationStart }
    : undefined;
  const invocationCommitted = invocationRange ? Math.max(0, lastTick - invocationRange.start) : undefined;
  const progress = invocationRange
    ? invocationRange.budget === 0
      ? lastTick >= invocationRange.target ? 1 : 0
      : Math.min(1, Math.max(0, (lastTick - invocationRange.start) / invocationRange.budget))
    : undefined;
  const generationMarkers = ticks.flatMap((tick, index) => {
    const previous = ticks[index - 1]?.generation;
    return tick.generation !== undefined && tick.generation !== previous ? [tick] : [];
  });

  return (
    <div className="tick-progress">
      <div className="tick-summary">
        <div><span>Committed tick</span><strong>{formatInteger(lastTick)}</strong></div>
        <div><span>Event seq</span><strong>{formatInteger(run.last_seq ?? ticks.at(-1)?.seq)}</strong></div>
        <div><span>Generation</span><strong>{run.generation !== undefined ? `G${run.generation}` : "—"}</strong></div>
        <div><span>Local rate</span><strong>{ticksPerMinute !== undefined ? `${formatNumber(ticksPerMinute, 1)} t/min` : "warming up"}</strong></div>
      </div>
      {invocationRange && progress !== undefined ? (
        <>
          <div className="progress-track" aria-label={`${Math.round(progress * 100)}% текущего invocation`}>
            <div className="progress-fill" style={{ width: `${Math.max(1, progress * 100)}%` }} />
            {generationMarkers.flatMap((tick) => tick.tick >= invocationRange.start && tick.tick <= invocationRange.target ? [(
              <span
                className="generation-marker"
                key={`${tick.tick}-${tick.generation}`}
                style={{ left: `${invocationRange.budget === 0 ? 100 : Math.min(100, Math.max(0, (tick.tick - invocationRange.start) / invocationRange.budget * 100))}%` }}
                title={`G${tick.generation} · committed tick ${tick.tick}`}
              />
            )] : [])}
          </div>
          <div className="progress-scale">
            <span>tick {formatInteger(invocationRange.start)}</span>
            <span>{formatInteger(invocationCommitted)} / {formatInteger(invocationRange.budget)} invocation commits · {Math.round(progress * 100)}%</span>
            <span>tick {formatInteger(invocationRange.target)}</span>
          </div>
        </>
      ) : (
        <div className="durable-cursor" aria-label={`Durable committed tick ${lastTick}`}>
          <span><i /> durable cursor</span>
          <strong>tick {formatInteger(lastTick)}</strong>
          <small>{run.max_ticks !== undefined ? `Бюджет текущего invocation: +${formatInteger(run.max_ticks)} commits` : "Invocation target сервером не задан"}</small>
        </div>
      )}
    </div>
  );
}
