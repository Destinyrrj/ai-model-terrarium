export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject {
  [key: string]: JsonValue;
}

export interface JsonSchema {
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  type?: string | string[];
  title?: string;
  description?: string;
  default?: JsonValue;
  enum?: JsonValue[];
  const?: JsonValue;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  items?: JsonSchema;
  anyOf?: JsonSchema[];
  oneOf?: JsonSchema[];
  allOf?: JsonSchema[];
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  minLength?: number;
  maxLength?: number;
  minItems?: number;
  maxItems?: number;
  pattern?: string;
  format?: string;
  additionalProperties?: boolean | JsonSchema;
  [key: string]: unknown;
}

export type RunStatus =
  | "running"
  | "external"
  | "initializing"
  | "stopping"
  | "resumable"
  | "completed"
  | "invalid"
  | "incomplete"
  | "failed"
  | "unknown";

export interface RunSummary {
  name: string;
  run_id?: string;
  status: RunStatus;
  config_name?: string;
  current_tick?: number;
  max_ticks?: number;
  invocation_start_tick?: number;
  invocation_target_tick?: number;
  target_generations?: number;
  generation?: number;
  last_seq?: number;
  started_at?: string;
  pid?: number;
  error?: string;
  raw: Record<string, unknown>;
}

export interface ConfigSummary {
  name: string;
  modified_at?: string;
  size?: number;
}

export interface ValidationIssue {
  loc: Array<string | number>;
  message: string;
  type?: string;
}

export interface ValidationResult {
  valid: boolean;
  issues: ValidationIssue[];
  config?: Record<string, unknown>;
  raw: unknown;
}

export interface TerrariumEvent {
  seq: number;
  tick?: number;
  type: string;
  payload: Record<string, unknown>;
  hash?: string;
  raw: Record<string, unknown>;
}

export interface AgentRecord {
  id: string;
  lineage_id?: string;
  generation?: number;
  born_tick?: number;
  died_tick?: number;
  location?: string;
  cause?: string;
  status?: string;
  raw: Record<string, unknown>;
}

export interface TickRecord {
  tick: number;
  seq?: number;
  generation?: number;
  committed?: boolean;
  raw: Record<string, unknown>;
}

export interface WorldAgent {
  id: string;
  alive?: boolean;
  x?: number;
  y?: number;
  location?: string;
  health?: number;
  hunger?: number;
  generation?: number;
}

export interface WorldLocation {
  id: string;
  x?: number;
  y?: number;
  food?: number;
  kind?: string;
}

export interface WorldSnapshot {
  tick?: number;
  width?: number;
  height?: number;
  agents: WorldAgent[];
  locations: WorldLocation[];
  edges: Array<{ source: string; target: string }>;
  raw: Record<string, unknown>;
}

export interface JobResponse {
  job_id?: string;
  status?: string;
  result?: unknown;
  error?: string;
  raw: unknown;
}

export interface StreamCallbacks {
  onEvent: (event: TerrariumEvent) => void;
  onGap?: (afterSeq?: number, latestSeq?: number) => void;
  onState?: (state: "connecting" | "open" | "closed" | "error") => void;
}
