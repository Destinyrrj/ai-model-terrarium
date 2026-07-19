import { useEffect, useRef, useState } from "react";
import { asRecord, asString } from "../../api/normalize";
import { api } from "../../api/client";
import type { JobResponse, RunStatus } from "../../api/types";
import { SafeJson, SanitizedText } from "../Primitives";

const tools = [
  { id: "verify", label: "Verify", detail: "Проверить hash-chain и проекции", tone: "primary" },
  { id: "rebuild", label: "Rebuild", detail: "Перестроить SQLite-проекцию", tone: "default" },
  { id: "replay", label: "Replay", detail: "Проверить детерминизм исполнения", tone: "default" },
  { id: "measure", label: "Measure", detail: "Рассчитать survival и behavior", tone: "default" },
  { id: "viewer-export", label: "Viewer export", detail: "Собрать статический post-hoc viewer", tone: "default" },
] as const;

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    let timer = 0;
    const abort = () => {
      window.clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      const error = new Error("Job polling cancelled");
      error.name = "AbortError";
      reject(error);
    };
    const finish = () => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    timer = window.setTimeout(finish, milliseconds);
    if (signal?.aborted) abort();
    else signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function waitForJob(initial: JobResponse, signal?: AbortSignal): Promise<JobResponse> {
  const terminal = ["completed", "succeeded", "failed", "error", "cancelled"];
  if (!initial.job_id || !initial.status || terminal.includes(initial.status)) return initial;
  let current = initial;
  while (!signal?.aborted) {
    await delay(1_000, signal);
    current = await api.job(initial.job_id);
    if (!current.status || terminal.includes(current.status)) return current;
  }
  return current;
}

export function AuditPanel({ runName, status, onToolCompleted }: {
  runName: string;
  status: RunStatus;
  onToolCompleted?: (tool: string, result: JobResponse) => void;
}): React.JSX.Element {
  const [active, setActive] = useState<string>();
  const [results, setResults] = useState<Record<string, JobResponse>>({});
  const [stderr, setStderr] = useState<string>();
  const controllers = useRef(new Set<AbortController>());
  const running = status === "running" || status === "external" || status === "initializing" || status === "stopping";

  useEffect(() => {
    const activeControllers = controllers.current;
    const controller = new AbortController();
    activeControllers.add(controller);
    void api.toolJobs(runName).then((jobs) => {
      if (controller.signal.aborted) return;
      const restored: Record<string, JobResponse> = {};
      for (const job of jobs) {
        const tool = asString(asRecord(job.raw).tool);
        if (tool) restored[tool] = job;
      }
      setResults(restored);
      const pending = [...jobs].reverse().find((job) =>
        ["queued", "running"].includes(job.status ?? "")
      );
      const tool = pending ? asString(asRecord(pending.raw).tool) : undefined;
      if (!pending || !tool) return;
      setActive(tool);
      void waitForJob(pending, controller.signal).then((result) => {
        if (!controller.signal.aborted) {
          setResults((values) => ({ ...values, [tool]: result }));
          setActive(undefined);
        }
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setResults((values) => ({
            ...values,
            [tool]: { error: reason instanceof Error ? reason.message : String(reason), raw: null },
          }));
          setActive(undefined);
        }
      });
    }).catch(() => {
      // Job discovery is additive; individual tool actions remain available.
    });
    return () => {
      controller.abort();
      activeControllers.delete(controller);
    };
  }, [runName]);

  useEffect(() => () => {
    for (const controller of controllers.current) controller.abort();
    controllers.current.clear();
  }, []);

  const execute = async (tool: string) => {
    const controller = new AbortController();
    controllers.current.add(controller);
    setActive(tool);
    try {
      const result = await waitForJob(await api.tool(runName, tool), controller.signal);
      if (controller.signal.aborted) return;
      setResults((values) => ({ ...values, [tool]: result }));
      if (!result.error && ["completed", "succeeded"].includes(result.status ?? "")) {
        onToolCompleted?.(tool, result);
      }
    } catch (reason) {
      if (controller.signal.aborted) return;
      setResults((values) => ({ ...values, [tool]: { error: reason instanceof Error ? reason.message : String(reason), raw: null } }));
    } finally {
      controllers.current.delete(controller);
      if (!controller.signal.aborted) setActive(undefined);
    }
  };

  const loadStderr = async () => {
    setActive("stderr");
    try { setStderr(await api.stderr(runName)); }
    catch (reason) { setStderr(reason instanceof Error ? reason.message : String(reason)); }
    finally { setActive(undefined); }
  };

  return (
    <div className="audit-layout">
      {running ? (
        <div className="notice notice-warning"><strong>Audit tools заблокированы активным writer-lock.</strong><span>Остановите процесс и дождитесь статуса resumable или completed. Сервер вернёт 409 при гонке.</span></div>
      ) : null}
      <div className="audit-tools">
        {tools.map((tool) => (
          <article className="audit-tool" key={tool.id}>
            <div><span className="tool-mark" aria-hidden="true">{tool.id === "verify" ? "✓" : "⌘"}</span><span><strong>{tool.label}</strong><small>{tool.detail}</small></span></div>
            <button type="button" className={`button button-small ${tool.tone === "primary" ? "button-primary" : "button-ghost"}`} disabled={running || Boolean(active)} onClick={() => void execute(tool.id)}>{active === tool.id ? "Выполняется…" : "Запустить"}</button>
            {results[tool.id] ? (
              <div className={results[tool.id]?.error ? "tool-result failed" : "tool-result"}>
                <span>{results[tool.id]?.error ? "error" : results[tool.id]?.status ?? "result"}</span>
                {results[tool.id]?.error ? <SanitizedText value={results[tool.id]?.error} /> : <pre><SafeJson value={results[tool.id]?.result ?? results[tool.id]?.raw} /></pre>}
              </div>
            ) : null}
          </article>
        ))}
      </div>
      <section className="stderr-panel">
        <header><div><h3>Process stderr</h3><p>Санитизированный ограниченный хвост, хранящийся вне event stream.</p></div><button type="button" className="button button-small button-ghost" disabled={Boolean(active)} onClick={() => void loadStderr()}>{active === "stderr" ? "Читаем…" : "Обновить хвост"}</button></header>
        <pre>{stderr === undefined ? <span className="muted">Хвост ещё не загружен.</span> : <SanitizedText value={stderr} limit={32_000} />}</pre>
      </section>
    </div>
  );
}
