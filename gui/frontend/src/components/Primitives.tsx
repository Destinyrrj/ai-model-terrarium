import { useState } from "react";
import { setToken } from "../api/client";
import { Link } from "../router";

export function SanitizedText({ value, limit = 16_384, className }: {
  value: unknown;
  limit?: number;
  className?: string;
}): React.JSX.Element {
  const source = typeof value === "string" ? value : value == null ? "" : String(value);
  const clean = source.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, "�");
  const display = clean.length > limit ? `${clean.slice(0, limit)}… [truncated]` : clean;
  return <span className={className}>{display}</span>;
}

export function SafeJson({ value, limit = 20_000 }: { value: unknown; limit?: number }): React.JSX.Element {
  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return <SanitizedText value={text} limit={limit} />;
}

export function StatusChip({ status }: { status: string }): React.JSX.Element {
  const normalized = status.toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  return (
    <span className={`status-chip status-${normalized}`}>
      <span className="status-dot" aria-hidden="true" />
      {status}
    </span>
  );
}

export function EmptyState({ title, detail }: { title: string; detail?: string }): React.JSX.Element {
  return (
    <div className="empty-state">
      <div className="empty-mark" aria-hidden="true">∅</div>
      <strong>{title}</strong>
      {detail ? <p>{detail}</p> : null}
    </div>
  );
}

export function ErrorNotice({ error, retry }: { error: Error; retry?: () => void }): React.JSX.Element {
  return (
    <div className="notice notice-error" role="alert">
      <div>
        <strong>Запрос не выполнен</strong>
        <SanitizedText value={error.message} />
      </div>
      {retry ? <button type="button" className="button button-small" onClick={retry}>Повторить</button> : null}
    </div>
  );
}

export function LoadingBlock({ label = "Загрузка данных" }: { label?: string }): React.JSX.Element {
  return (
    <div className="loading-block" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function MetricCard({ label, value, detail, tone = "default" }: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  tone?: "default" | "blue" | "gold" | "orange";
}): React.JSX.Element {
  return (
    <article className={`metric-card metric-${tone}`}>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
      {detail ? <span className="metric-detail">{detail}</span> : null}
    </article>
  );
}

export function Panel({ title, subtitle, actions, children, className }: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}): React.JSX.Element {
  return (
    <section className={className ? `panel ${className}` : "panel"}>
      <header className="panel-header">
        <div>
          <h2>{title}</h2>
          {subtitle ? <p>{subtitle}</p> : null}
        </div>
        {actions ? <div className="panel-actions">{actions}</div> : null}
      </header>
      <div className="panel-body">{children}</div>
    </section>
  );
}

export function TokenDialog(): React.JSX.Element {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  return (
    <>
      <button type="button" className="icon-button" title="Изменить токен сессии" onClick={() => setOpen(true)}>
        <span aria-hidden="true">⌁</span>
        <span className="sr-only">Изменить токен</span>
      </button>
      {open ? (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setOpen(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="token-title" onMouseDown={(event) => event.stopPropagation()}>
            <h2 id="token-title">Токен локальной сессии</h2>
            <p>Токен хранится только в sessionStorage этой вкладки и добавляется к API-запросам.</p>
            <label className="field">
              <span>Bearer token</span>
              <input type="password" autoComplete="off" value={value} onChange={(event) => setValue(event.target.value)} autoFocus />
            </label>
            <div className="modal-actions">
              <button type="button" className="button button-ghost" onClick={() => setOpen(false)}>Отмена</button>
              <button type="button" className="button button-primary" onClick={() => { setToken(value); setOpen(false); window.location.reload(); }}>Сохранить</button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}

export function AppShell({ pathname, children }: { pathname: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <div className="app-shell">
      <header className="topbar">
        <Link href="/" className="brand" title="AI Model Terrarium">
          <span className="brand-symbol" aria-hidden="true"><i /><i /><i /></span>
          <span>
            <strong>Terrarium</strong>
            <small>research console</small>
          </span>
        </Link>
        <nav className="main-nav" aria-label="Основная навигация">
          <Link href="/" className={pathname === "/" ? "active" : undefined}>Обзор</Link>
          <Link href="/configs" className={pathname.startsWith("/configs") ? "active" : undefined}>Конфигурации</Link>
        </nav>
        <div className="topbar-meta">
          <span className="local-indicator"><span /> 127.0.0.1</span>
          <TokenDialog />
        </div>
      </header>
      <main>{children}</main>
      <footer className="app-footer">
        <span>AI Model Terrarium · deterministic observation surface</span>
        <span>Время эксперимента: seq / tick</span>
      </footer>
    </div>
  );
}
