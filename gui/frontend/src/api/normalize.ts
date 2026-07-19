import type {
  AgentRecord,
  ConfigSummary,
  JsonObject,
  RunStatus,
  RunSummary,
  TerrariumEvent,
  TickRecord,
  ValidationIssue,
  ValidationResult,
  WorldAgent,
  WorldLocation,
  WorldSnapshot,
} from "./types";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

export function asString(value: unknown): string | undefined {
  return typeof value === "string" ? value : undefined;
}

export function asNumber(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

export function asBoolean(value: unknown): boolean | undefined {
  return typeof value === "boolean" ? value : undefined;
}

export function firstString(record: Record<string, unknown>, keys: string[]): string | undefined {
  for (const key of keys) {
    const value = asString(record[key]);
    if (value !== undefined) return value;
  }
  return undefined;
}

export function firstNumber(record: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = asNumber(record[key]);
    if (value !== undefined) return value;
  }
  return undefined;
}

export function unwrapArray(value: unknown, keys: string[] = ["items", "data", "results"]): unknown[] {
  if (Array.isArray(value)) return value;
  const record = asRecord(value);
  for (const key of keys) {
    const candidate = record[key];
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

export function normalizeStatus(value: unknown): RunStatus {
  const status = asString(value)?.toLowerCase();
  if (
    status === "running" ||
    status === "external" ||
    status === "initializing" ||
    status === "stopping" ||
    status === "resumable" ||
    status === "completed" ||
    status === "invalid" ||
    status === "incomplete" ||
    status === "failed"
  ) {
    return status;
  }
  return "unknown";
}

export function normalizeRun(value: unknown): RunSummary {
  const envelope = asRecord(value);
  const run = isRecord(envelope.run) ? envelope.run : envelope;
  const process = asRecord(envelope.process);
  const raw = { ...run, ...Object.fromEntries(Object.entries(process).filter(([, item]) => item !== null && item !== undefined)) };
  const name = firstString(raw, ["name", "run_name", "directory", "id", "run_id"]) ?? "unknown-run";
  return {
    name,
    run_id: firstString(raw, ["run_id", "id"]),
    status: normalizeStatus(raw.status),
    config_name: firstString(raw, ["config_name", "config"]),
    current_tick: firstNumber(raw, ["current_tick", "tick", "last_tick"]),
    max_ticks: firstNumber(raw, ["max_ticks", "tick_limit"]),
    invocation_start_tick: firstNumber(raw, ["invocation_start_tick"]),
    invocation_target_tick: firstNumber(raw, ["invocation_target_tick"]),
    target_generations: firstNumber(raw, ["target_generations", "generations", "target_generation"]),
    generation: firstNumber(raw, ["generation", "minimum_generation", "current_generation"]),
    last_seq: firstNumber(raw, ["last_seq", "seq", "event_seq"]),
    started_at: firstString(process, ["started_at", "created_at"]) ?? firstString(raw, ["started_at", "created_at"]),
    pid: firstNumber(raw, ["pid"]),
    error: firstString(raw, ["error", "message"]),
    raw,
  };
}

export function normalizeRuns(value: unknown): RunSummary[] {
  return unwrapArray(value, ["runs", "items", "data", "results"]).map(normalizeRun);
}

export function normalizeConfigs(value: unknown): ConfigSummary[] {
  const values = Array.isArray(value)
    ? value
    : unwrapArray(value, ["configs", "items", "data", "results"]);
  return values.flatMap((item) => {
    if (typeof item === "string") return [{ name: item }];
    const record = asRecord(item);
    const name = firstString(record, ["name", "config_name", "id"]);
    return name
      ? [{ name, modified_at: asString(record.modified_at), size: asNumber(record.size) ?? asNumber(record.size_bytes) }]
      : [];
  });
}

export function normalizeValidation(value: unknown): ValidationResult {
  const outer = asRecord(value);
  const record = isRecord(outer.detail) ? outer.detail : outer;
  const issueValues = unwrapArray(record.errors ?? record.issues ?? [], ["errors", "issues", "items"]);
  const issues: ValidationIssue[] = issueValues.map((item) => {
    const issue = asRecord(item);
    const rawLoc = issue.loc ?? issue.path ?? [];
    const loc = Array.isArray(rawLoc)
      ? rawLoc.filter((part): part is string | number => typeof part === "string" || typeof part === "number")
      : typeof rawLoc === "string"
        ? rawLoc.split(".").filter(Boolean)
        : [];
    return {
      loc,
      message: firstString(issue, ["message", "msg", "detail"]) ?? "Validation error",
      type: asString(issue.type),
    };
  });
  const valid = asBoolean(record.valid) ?? asBoolean(record.ok) ?? issues.length === 0;
  const config = isRecord(record.config) ? record.config : isRecord(record.data) ? record.data : undefined;
  return { valid, issues, config, raw: value };
}

export function normalizeEvent(value: unknown): TerrariumEvent | null {
  const outer = asRecord(value);
  const candidate = isRecord(outer.event) ? outer.event : isRecord(outer.data) ? outer.data : outer;
  const payload = isRecord(candidate.payload)
    ? candidate.payload
    : isRecord(candidate.fields)
      ? candidate.fields
      : {};
  const seq = firstNumber(candidate, ["seq", "event_seq"]) ?? firstNumber(outer, ["seq", "event_seq"]);
  const type = firstString(candidate, ["type", "event_type", "kind"]);
  if (seq === undefined || type === undefined) return null;
  return {
    seq,
    tick: firstNumber(candidate, ["tick", "tick_id"]) ?? firstNumber(payload, ["tick", "tick_id"]),
    type,
    payload,
    hash: firstString(candidate, ["hash", "event_hash"]),
    raw: candidate,
  };
}

export function normalizeEvents(value: unknown): TerrariumEvent[] {
  return unwrapArray(value, ["events", "items", "data", "results"])
    .map(normalizeEvent)
    .filter((event): event is TerrariumEvent => event !== null)
    .sort((left, right) => left.seq - right.seq);
}

export function normalizeAgents(value: unknown): AgentRecord[] {
  return unwrapArray(value, ["agents", "items", "data", "results"]).flatMap((item) => {
    const raw = asRecord(item);
    const id = firstString(raw, ["id", "agent_id"]);
    if (!id) return [];
    return [{
      id,
      lineage_id: firstString(raw, ["lineage_id", "lineage"]),
      generation: firstNumber(raw, ["generation"]),
      born_tick: firstNumber(raw, ["born_tick", "birth_tick", "start_tick"]),
      died_tick: firstNumber(raw, ["died_tick", "death_tick", "end_tick"]),
      location: firstString(raw, ["location", "location_id"]),
      cause: firstString(raw, ["cause", "death_cause"]),
      status: firstString(raw, ["status"]),
      raw,
    }];
  });
}

export function normalizeTicks(value: unknown): TickRecord[] {
  const root = asRecord(value);
  const markers = unwrapArray(root.generation_markers, ["markers", "items"])
    .map(asRecord)
    .flatMap((marker) => {
      const generation = firstNumber(marker, ["generation"]);
      const started = firstNumber(marker, ["started_tick", "tick"]);
      return generation !== undefined && started !== undefined ? [{ generation, started }] : [];
    })
    .sort((left, right) => left.started - right.started);
  return unwrapArray(value, ["ticks", "items", "data", "results"]).flatMap((item) => {
    const raw = asRecord(item);
    const tick = firstNumber(raw, ["tick", "tick_id"]);
    if (tick === undefined) return [];
    return [{
      tick,
      seq: firstNumber(raw, ["seq", "event_seq", "commit_seq"]),
      generation: firstNumber(raw, ["generation", "minimum_generation"])
        ?? [...markers].reverse().find((marker) => marker.started <= tick)?.generation,
      committed: asBoolean(raw.committed),
      raw,
    }];
  });
}

function normalizeWorldAgent(value: unknown): WorldAgent | null {
  const raw = asRecord(value);
  const id = firstString(raw, ["id", "agent_id"]);
  if (!id) return null;
  return {
    id,
    alive: asBoolean(raw.alive),
    x: firstNumber(raw, ["x", "col"]),
    y: firstNumber(raw, ["y", "row"]),
    location: firstString(raw, ["location", "location_id", "loc"]),
    health: firstNumber(raw, ["health", "hp"]),
    hunger: firstNumber(raw, ["hunger"]),
    generation: firstNumber(raw, ["generation"]),
  };
}

function normalizeWorldLocation(value: unknown, fallbackId?: string): WorldLocation | null {
  const raw = asRecord(value);
  const id = firstString(raw, ["id", "location_id", "name"]) ?? fallbackId;
  if (!id) return null;
  return {
    id,
    x: firstNumber(raw, ["x", "col"]),
    y: firstNumber(raw, ["y", "row"]),
    food: firstNumber(raw, ["food", "food_units"]),
    kind: firstString(raw, ["kind", "type"]),
  };
}

export function normalizeWorld(value: unknown): WorldSnapshot {
  const envelope = asRecord(value);
  const raw = isRecord(envelope.world) ? envelope.world : isRecord(envelope.state) ? envelope.state : envelope;
  const agentSource = raw.agents;
  const agentItems = Array.isArray(agentSource)
    ? agentSource
    : isRecord(agentSource)
      ? Object.entries(agentSource).map(([id, agent]) => ({ id, ...asRecord(agent) }))
      : [];
  const locationSource = raw.locations ?? raw.nodes;
  const locationItems: Array<[string | undefined, unknown]> = Array.isArray(locationSource)
    ? locationSource.map((location) => [undefined, location])
    : isRecord(locationSource)
      ? Object.entries(locationSource)
      : [];
  const explicitEdges = unwrapArray(raw.edges ?? raw.paths ?? [], ["edges", "paths", "items"]).flatMap((item) => {
    const edge = asRecord(item);
    const source = firstString(edge, ["source", "from"]);
    const target = firstString(edge, ["target", "to"]);
    return source && target ? [{ source, target }] : [];
  });
  const neighborEdges = locationItems.flatMap(([fallbackId, item]) => {
    const location = asRecord(item);
    const source = firstString(location, ["id", "location_id", "name"]) ?? fallbackId;
    if (!source || !Array.isArray(location.neighbors)) return [];
    return location.neighbors.flatMap((target) => typeof target === "string" ? [{ source, target }] : []);
  });
  const resources = asRecord(raw.resources);
  const normalizedLocations = locationItems
    .map(([id, location]) => {
      const normalized = normalizeWorldLocation(location, id);
      if (!normalized) return null;
      if (normalized.food === undefined) {
        const counts = asRecord(resources[normalized.id]);
        if (Object.keys(counts).length) {
          normalized.food = Object.values(counts)
            .reduce<number>((sum, count) => sum + (asNumber(count) ?? 0), 0);
        }
      }
      return normalized;
    })
    .filter((location): location is WorldLocation => location !== null);
  const edgeKeys = new Set<string>();
  const edges = [...explicitEdges, ...neighborEdges].filter((edge) => {
    const canonical = [edge.source, edge.target].sort().join("\u0000");
    if (edgeKeys.has(canonical)) return false;
    edgeKeys.add(canonical);
    return true;
  });
  return {
    tick: firstNumber(envelope, ["tick", "checkpoint_tick"]) ?? firstNumber(raw, ["tick"]),
    width: firstNumber(raw, ["width", "columns"]),
    height: firstNumber(raw, ["height", "rows"]),
    agents: agentItems.map(normalizeWorldAgent).filter((agent): agent is WorldAgent => agent !== null),
    locations: normalizedLocations,
    edges,
    raw,
  };
}

export function toJsonObject(value: unknown): JsonObject {
  return isRecord(value) ? (value as JsonObject) : {};
}
