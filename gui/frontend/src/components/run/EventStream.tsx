import { useMemo, useState } from "react";
import type { TerrariumEvent } from "../../api/types";
import { formatInteger } from "../../lib/format";
import { EmptyState, SafeJson, SanitizedText } from "../Primitives";

const FORBIDDEN_KEYS = new Set(["raw_text", "raw_response", "prompt", "stdout_raw", "stderr_raw"]);

function safeProjection(value: unknown, depth = 0): unknown {
  if (depth > 8) return "[depth limit]";
  if (typeof value === "string") return value.length > 8_192 ? `${value.slice(0, 8_192)}… [truncated]` : value;
  if (Array.isArray(value)) return value.slice(0, 128).map((item) => safeProjection(item, depth + 1));
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .filter(([key]) => !FORBIDDEN_KEYS.has(key.toLowerCase()))
        .slice(0, 128)
        .map(([key, item]) => [key, safeProjection(item, depth + 1)]),
    );
  }
  return value;
}

function EventRow({ event }: { event: TerrariumEvent }): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const keyValues = Object.entries(event.payload).slice(0, 4);
  return (
    <article className="event-row">
      <button type="button" className="event-main" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span className="event-seq">#{formatInteger(event.seq)}</span>
        <span className="event-tick">t{formatInteger(event.tick)}</span>
        <span className="event-type"><i aria-hidden="true" /><SanitizedText value={event.type} limit={128} /></span>
        <span className="event-preview">
          {keyValues.map(([key, value]) => (
            <span key={key}><b><SanitizedText value={key} limit={64} /></b>=<SanitizedText value={typeof value === "object" ? JSON.stringify(value) : value} limit={180} /></span>
          ))}
        </span>
        <span className="event-toggle" aria-hidden="true">{expanded ? "−" : "+"}</span>
      </button>
      {expanded ? <pre className="event-json"><SafeJson value={safeProjection(event.payload)} /></pre> : null}
    </article>
  );
}

export function EventStream({ events, connection, onLoadOlder, loadingOlder }: {
  events: TerrariumEvent[];
  connection: "connecting" | "open" | "closed" | "error";
  onLoadOlder?: () => void;
  loadingOlder?: boolean;
}): React.JSX.Element {
  const [typeFilter, setTypeFilter] = useState("all");
  const [query, setQuery] = useState("");
  const types = useMemo(() => [...new Set(events.map((event) => event.type))].sort(), [events]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return events.filter((event) => {
      if (typeFilter !== "all" && event.type !== typeFilter) return false;
      if (!needle) return true;
      const haystack = `${event.seq} ${event.tick ?? ""} ${event.type} ${JSON.stringify(safeProjection(event.payload))}`.toLowerCase();
      return haystack.includes(needle);
    });
  }, [events, query, typeFilter]);
  const windowed = filtered.slice(-300);

  return (
    <div className="event-stream">
      <div className="event-toolbar">
        <span className={`stream-state stream-${connection}`}><i />{connection === "open" ? "SSE live" : connection}</span>
        <label><span className="sr-only">Тип события</span><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">Все типы</option>{types.map((type) => <option key={type}>{type}</option>)}</select></label>
        <label className="event-search"><span aria-hidden="true">⌕</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="seq, tick, поле…" /></label>
        <code>{formatInteger(filtered.length)} / {formatInteger(events.length)}</code>
      </div>
      {onLoadOlder ? <button type="button" className="button button-small button-ghost load-older" onClick={onLoadOlder} disabled={loadingOlder}>{loadingOlder ? "Загружаем…" : "Загрузить более ранние события"}</button> : null}
      {filtered.length > windowed.length ? <p className="virtual-note">Для отзывчивости показаны последние {windowed.length} совпадений; фильтр применяется ко всему локальному буферу.</p> : null}
      <div className="event-list" role="log" aria-live="off">
        {windowed.map((event) => <EventRow key={event.seq} event={event} />)}
        {!windowed.length ? <EmptyState title="Нет совпадающих событий" detail={events.length ? "Измените фильтр." : "Ожидаем первый committed tick."} /> : null}
      </div>
    </div>
  );
}
