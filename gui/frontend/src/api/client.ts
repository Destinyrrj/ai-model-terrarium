import {
  asRecord,
  asString,
  normalizeConfigs,
  normalizeEvent,
  normalizeEvents,
  normalizeRun,
  normalizeRuns,
  normalizeValidation,
  unwrapArray,
} from "./normalize";
import { isRecord } from "./normalize";
import type {
  ConfigSummary,
  JobResponse,
  JsonSchema,
  RunSummary,
  StreamCallbacks,
  TerrariumEvent,
  ValidationResult,
} from "./types";

const API_ROOT = "/api/v1";
const TOKEN_KEY = "terrarium.gui.token";

export class ApiError extends Error {
  readonly status: number;
  readonly category?: string;
  readonly detail?: unknown;

  constructor(message: string, status: number, category?: string, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.category = category;
    this.detail = detail;
  }
}

function captureTokenFromUrl(): string | null {
  const url = new URL(window.location.href);
  const fragment = new URLSearchParams(url.hash.replace(/^#/, ""));
  const token = fragment.get("token");
  if (!token) return null;
  sessionStorage.setItem(TOKEN_KEY, token);
  fragment.delete("token");
  url.hash = fragment.toString();
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  return token;
}

let sessionToken = captureTokenFromUrl() ?? sessionStorage.getItem(TOKEN_KEY);

export function getToken(): string | null {
  return sessionToken;
}

export function setToken(value: string): void {
  const normalized = value.trim();
  sessionToken = normalized || null;
  if (sessionToken) sessionStorage.setItem(TOKEN_KEY, sessionToken);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function encodePathPart(value: string): string {
  return encodeURIComponent(value);
}

async function parseResponse(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) return response.json();
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export async function request<T = unknown>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (sessionToken) headers.set("Authorization", `Bearer ${sessionToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  headers.set("Accept", "application/json");
  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  const body = await parseResponse(response);
  if (!response.ok) {
    const record = asRecord(body);
    const detail = record.detail;
    const detailRecord = asRecord(detail);
    const message =
      asString(record.message) ??
      asString(record.error) ??
      asString(detailRecord.message) ??
      asString(detail) ??
      `${response.status} ${response.statusText}`;
    const category = asString(record.category) ?? asString(detailRecord.category) ?? asString(record.code);
    throw new ApiError(message, response.status, category, body);
  }
  return body as T;
}

export const api = {
  async configSchema(): Promise<JsonSchema> {
    const body = await request<unknown>("/schema/config");
    const record = asRecord(body);
    return (asRecord(record.schema ?? body) as JsonSchema);
  },

  async configs(): Promise<ConfigSummary[]> {
    return normalizeConfigs(await request<unknown>("/configs"));
  },

  async config(name: string): Promise<{ yaml: string; data?: Record<string, unknown> }> {
    const body = await request<unknown>(`/configs/${encodePathPart(name)}`);
    if (typeof body === "string") return { yaml: body };
    const record = asRecord(body);
    const yaml = asString(record.yaml) ?? asString(record.content) ?? asString(record.raw) ?? "";
    const data = asRecord(record.config ?? record.data);
    return { yaml, data: Object.keys(data).length ? data : undefined };
  },

  async saveConfig(name: string, yaml: string): Promise<unknown> {
    return request(`/configs/${encodePathPart(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "text/yaml; charset=utf-8" },
      body: yaml,
    });
  },

  async deleteConfig(name: string): Promise<void> {
    await request(`/configs/${encodePathPart(name)}`, { method: "DELETE" });
  },

  async validateConfig(yaml: string): Promise<ValidationResult> {
    try {
      const body = await request<unknown>("/configs/validate", {
        method: "POST",
        headers: { "Content-Type": "text/yaml; charset=utf-8" },
        body: yaml,
      });
      return normalizeValidation(body);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 422) return normalizeValidation(reason.detail);
      throw reason;
    }
  },

  async runs(): Promise<RunSummary[]> {
    return normalizeRuns(await request<unknown>("/runs"));
  },

  async run(name: string): Promise<RunSummary> {
    return normalizeRun(await request<unknown>(`/runs/${encodePathPart(name)}`));
  },

  async startRun(input: { config_name: string; run_name: string; max_ticks?: number }): Promise<unknown> {
    return request("/runs", { method: "POST", body: JSON.stringify(input) });
  },

  async stopRun(name: string): Promise<unknown> {
    return request(`/runs/${encodePathPart(name)}/stop`, { method: "POST" });
  },

  async resumeRun(name: string, maxTicks?: number): Promise<unknown> {
    return request(`/runs/${encodePathPart(name)}/resume`, {
      method: "POST",
      body: JSON.stringify(maxTicks === undefined ? {} : { max_ticks: maxTicks }),
    });
  },

  async stderr(name: string, limit = 32_000): Promise<string> {
    const body = await request<unknown>(`/runs/${encodePathPart(name)}/stderr?max_bytes=${limit}`);
    if (typeof body === "string") return body;
    const record = asRecord(body);
    return asString(record.stderr) ?? asString(record.text) ?? "";
  },

  async events(name: string, afterSeq?: number, limit = 500, tail = false): Promise<TerrariumEvent[]> {
    const query = new URLSearchParams({ limit: String(limit) });
    if (afterSeq !== undefined) query.set("after_seq", String(afterSeq));
    if (tail) query.set("tail", "true");
    return normalizeEvents(
      await request<unknown>(`/runs/${encodePathPart(name)}/events?${query.toString()}`),
    );
  },

  async eventsBefore(name: string, beforeSeq: number, limit = 500): Promise<TerrariumEvent[]> {
    const query = new URLSearchParams({
      before_seq: String(beforeSeq),
      limit: String(limit),
    });
    return normalizeEvents(
      await request<unknown>(`/runs/${encodePathPart(name)}/events?${query.toString()}`),
    );
  },

  async runData(name: string, path: string, query?: URLSearchParams): Promise<unknown> {
    const suffix = query && query.size ? `?${query.toString()}` : "";
    return request(`/runs/${encodePathPart(name)}/${path}${suffix}`);
  },

  async tool(name: string, tool: string, options: Record<string, unknown> = {}): Promise<JobResponse> {
    const body = await request<unknown>(
      `/runs/${encodePathPart(name)}/tools/${encodePathPart(tool)}`,
      { method: "POST", body: JSON.stringify(options) },
    );
    const envelope = asRecord(body);
    const record = isRecord(envelope.job) ? envelope.job : envelope;
    return {
      job_id: asString(record.job_id) ?? asString(record.id),
      status: asString(record.status),
      result: record.result ?? record.output,
      error: asString(record.error),
      raw: body,
    };
  },

  async job(jobId: string): Promise<JobResponse> {
    const body = await request<unknown>(`/tool-jobs/${encodePathPart(jobId)}`);
    const envelope = asRecord(body);
    const record = isRecord(envelope.job) ? envelope.job : envelope;
    return {
      job_id: asString(record.job_id) ?? asString(record.id) ?? jobId,
      status: asString(record.status),
      result: record.result ?? record.output,
      error: asString(record.error),
      raw: body,
    };
  },

  async toolJobs(name: string): Promise<JobResponse[]> {
    const body = await request<unknown>(`/runs/${encodePathPart(name)}/tool-jobs`);
    return unwrapArray(body, ["jobs", "items"]).map((value) => {
      const record = asRecord(value);
      return {
        job_id: asString(record.job_id) ?? asString(record.id),
        status: asString(record.status),
        result: record.result ?? record.output,
        error: asString(record.error),
        raw: value,
      };
    });
  },
};

export function subscribeRunEvents(
  name: string,
  callbacks: StreamCallbacks,
  afterSeq?: number,
): () => void {
  const query = new URLSearchParams();
  if (sessionToken) query.set("access_token", sessionToken);
  if (afterSeq !== undefined) query.set("after_seq", String(afterSeq));
  const suffix = query.size ? `?${query.toString()}` : "";
  callbacks.onState?.("connecting");
  const source = new EventSource(
    `${API_ROOT}/runs/${encodePathPart(name)}/stream${suffix}`,
  );

  const deliver = (message: MessageEvent<string>): void => {
    try {
      const event = normalizeEvent(JSON.parse(message.data) as unknown);
      if (event) callbacks.onEvent(event);
    } catch {
      // The stream remains usable after a malformed or non-JSON diagnostic frame.
    }
  };
  const gap = (message: MessageEvent<string>): void => {
    try {
      const record = asRecord(JSON.parse(message.data) as unknown);
      callbacks.onGap?.(
        Number(record.after_seq ?? record.last_seq),
        Number(record.latest_seq),
      );
    } catch {
      callbacks.onGap?.();
    }
  };

  source.onopen = () => callbacks.onState?.("open");
  source.onerror = () => callbacks.onState?.("error");
  source.onmessage = deliver;
  source.addEventListener("event", deliver as EventListener);
  source.addEventListener("tick", deliver as EventListener);
  source.addEventListener("gap", gap as EventListener);

  return () => {
    source.close();
    callbacks.onState?.("closed");
  };
}
