import type { EChartsOption } from "echarts";
import { useEffect, useMemo, useState } from "react";
import { asRecord, asString, firstNumber, unwrapArray } from "../../api/normalize";
import type { AgentRecord } from "../../api/types";
import { formatInteger, shortId } from "../../lib/format";
import { baseAxis, baseGrid, chartTheme, EChart } from "../EChart";
import { EmptyState } from "../Primitives";

interface TreeDatum {
  name: string;
  value?: string | number;
  children?: TreeDatum[];
  itemStyle?: { color?: string; borderColor?: string };
  lineStyle?: { color?: string; type?: "solid" | "dashed" | "dotted" };
}

function safeTree(value: unknown, depth = 0): TreeDatum | null {
  if (depth > 128) return null;
  const raw = asRecord(value);
  const name = asString(raw.name) ?? asString(raw.id) ?? asString(raw.agent_id);
  if (!name) return null;
  const children = Array.isArray(raw.children)
    ? raw.children.map((child) => safeTree(child, depth + 1)).filter((child): child is TreeDatum => child !== null)
    : [];
  return { name, value: firstNumber(raw, ["generation", "tick"]) ?? asString(raw.cause), children };
}

function boundaryParentEdges(value: unknown): Array<{ source: string; target: string }> {
  const items: Array<{ value: unknown; target?: string }> = Array.isArray(value)
    ? value.map((item) => ({ value: item }))
    : Object.entries(asRecord(value)).map(([target, item]) => ({ value: item, target }));
  return items.flatMap(({ value: item, target: fallbackTarget }) => {
    if (typeof item === "string" && fallbackTarget) return [{ source: item, target: fallbackTarget }];
    const edge = asRecord(item);
    const source = asString(edge.source)
      ?? asString(edge.parent)
      ?? asString(edge.parent_id)
      ?? asString(edge.parent_agent_id)
      ?? asString(edge.predecessor_id);
    const target = asString(edge.target)
      ?? asString(edge.child)
      ?? asString(edge.child_id)
      ?? asString(edge.agent_id)
      ?? fallbackTarget;
    return source && target ? [{ source, target }] : [];
  });
}

function lineageTree(value: unknown): TreeDatum | null {
  const root = asRecord(value);
  if (root.tree) return safeTree(root.tree);
  if (root.name && root.children) return safeTree(root);
  const nodes = unwrapArray(value, ["nodes", "lineage", "lineages", "agents", "items", "data", "results"]).map(asRecord);
  if (!nodes.length) return null;

  const nodeById = new Map<string, TreeDatum>();
  const rawById = new Map<string, Record<string, unknown>>();
  nodes.forEach((node) => {
    const id = asString(node.id) ?? asString(node.agent_id) ?? asString(node.lineage_id);
    if (!id || nodeById.has(id)) return;
    const generation = firstNumber(node, ["generation"]);
    const cause = asString(node.cause) ?? asString(node.death_cause);
    nodeById.set(id, {
      name: shortId(id, 18),
      value: generation !== undefined ? `G${generation}${cause ? ` · ${cause}` : ""}` : cause,
      children: [],
      itemStyle: { color: cause ? chartTheme.orange : chartTheme.blue, borderColor: chartTheme.dark },
    });
    rawById.set(id, node);
  });

  const edges = unwrapArray(root.edges ?? [], ["edges", "items"]);
  const explicitParents = new Map<string, string>();
  edges.forEach((item) => {
    const edge = asRecord(item);
    const source = asString(edge.source) ?? asString(edge.parent);
    const target = asString(edge.target) ?? asString(edge.child);
    if (source && target) explicitParents.set(target, source);
  });
  boundaryParentEdges(root.boundary_parents).forEach(({ source, target }) => {
    if (!nodeById.has(source)) {
      nodeById.set(source, {
        name: `↥ ${shortId(source, 16)}`,
        value: "parent outside loaded server page",
        children: [],
        itemStyle: { color: chartTheme.gold, borderColor: chartTheme.dark },
        lineStyle: { color: chartTheme.gold, type: "dashed" },
      });
    }
    explicitParents.set(target, source);
  });
  rawById.forEach((node, id) => {
    const parent = asString(node.parent_id) ?? asString(node.parent_agent_id) ?? asString(node.predecessor_id);
    if (parent) explicitParents.set(id, parent);
  });

  const attached = new Set<string>();
  explicitParents.forEach((parent, child) => {
    const parentNode = nodeById.get(parent);
    const childNode = nodeById.get(child);
    if (parentNode && childNode && parent !== child) {
      parentNode.children?.push(childNode);
      attached.add(child);
    }
  });

  // When parent links are absent, generation order inside a lineage is the explicit scientific fallback.
  const groups = new Map<string, Array<{ id: string; raw: Record<string, unknown> }>>();
  rawById.forEach((raw, id) => {
    if (attached.has(id) || explicitParents.has(id)) return;
    const lineage = asString(raw.lineage_id) ?? "unassigned";
    const group = groups.get(lineage) ?? [];
    group.push({ id, raw });
    groups.set(lineage, group);
  });
  const children: TreeDatum[] = [];
  groups.forEach((group, lineage) => {
    group.sort((left, right) => (firstNumber(left.raw, ["generation"]) ?? 0) - (firstNumber(right.raw, ["generation"]) ?? 0));
    if (group.length > 1) {
      for (let index = 1; index < group.length; index += 1) {
        const parent = nodeById.get(group[index - 1]?.id ?? "");
        const child = nodeById.get(group[index]?.id ?? "");
        if (parent && child) parent.children?.push(child);
      }
    }
    const first = nodeById.get(group[0]?.id ?? "");
    if (first) children.push({ name: lineage, children: [first], itemStyle: { color: chartTheme.gold } });
  });
  nodeById.forEach((node, id) => {
    if (!attached.has(id) && ![...children].some((child) => child.children?.includes(node))) {
      const hasParent = explicitParents.has(id) && nodeById.has(explicitParents.get(id) ?? "");
      if (!hasParent) children.push(node);
    }
  });
  return { name: "lineages", children };
}

export function LineageTree({ data }: { data: unknown }): React.JSX.Element {
  const tree = lineageTree(data);
  if (!tree) return <EmptyState title="Линии ещё не сформированы" detail="Нужны записи agent_spawned или read-model lineage." />;
  const option: EChartsOption = {
    tooltip: {
      trigger: "item",
      triggerOn: "mousemove",
      renderMode: "richText",
      backgroundColor: "#102320",
      borderColor: "rgba(139,183,168,.35)",
      textStyle: { color: chartTheme.ink },
    },
    series: [{
      type: "tree",
      data: [tree],
      top: "4%",
      left: "8%",
      bottom: "4%",
      right: "22%",
      symbol: "circle",
      symbolSize: 9,
      orient: "LR",
      roam: true,
      expandAndCollapse: true,
      initialTreeDepth: 5,
      lineStyle: { color: "rgba(79,180,194,.42)", width: 1.5, curveness: .35 },
      itemStyle: { color: chartTheme.blue, borderColor: chartTheme.dark, borderWidth: 2 },
      label: { position: "left", verticalAlign: "middle", align: "right", color: chartTheme.muted, fontSize: 11 },
      leaves: { label: { position: "right", align: "left", color: chartTheme.ink } },
      emphasis: { focus: "descendant" },
    }],
  };
  return <EChart option={option} height={520} ariaLabel="Дерево поколений агентов, сгруппированное по линиям" />;
}

interface LegacyNode {
  id: string;
  label: string;
  channel: string;
  parents: string[];
  author?: string;
}

function stringArray(value: unknown): string[] {
  if (Array.isArray(value)) return value.filter((item): item is string => typeof item === "string");
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (Array.isArray(parsed)) return parsed.filter((item): item is string => typeof item === "string");
    } catch {
      return [];
    }
  }
  return [];
}

function legacyNodes(value: unknown): LegacyNode[] {
  return unwrapArray(value, ["legacies", "nodes", "items", "data", "results"]).flatMap((item) => {
    const raw = asRecord(item);
    const id = asString(raw.id) ?? asString(raw.legacy_id);
    if (!id) return [];
    const text = asString(raw.text) ?? asString(raw.summary) ?? "legacy";
    return [{
      id,
      label: text.length > 38 ? `${text.slice(0, 38)}…` : text,
      channel: asString(raw.channel) ?? "unknown",
      parents: stringArray(raw.parent_legacy_ids ?? raw.parent_legacy_ids_json ?? raw.parents),
      author: asString(raw.author_agent_id) ?? asString(raw.author),
    }];
  });
}

export function LegacyDag({ data }: { data: unknown }): React.JSX.Element {
  const values = legacyNodes(data);
  if (!values.length) return <EmptyState title="Legacy DAG пуст" detail="Письменные и устные наследия появятся после смены поколений." />;
  const channels = [...new Set(values.map((node) => node.channel))];
  const option: EChartsOption = {
    color: [...chartTheme.palette],
    tooltip: {
      trigger: "item",
      renderMode: "richText",
      backgroundColor: "#102320",
      borderColor: "rgba(139,183,168,.35)",
      textStyle: { color: chartTheme.ink },
    },
    legend: { data: channels, top: 0, textStyle: { color: chartTheme.muted } },
    series: [{
      type: "graph",
      layout: "force",
      roam: true,
      draggable: false,
      categories: channels.map((name) => ({ name })),
      data: values.map((node) => ({
        id: node.id,
        name: node.label,
        value: node.author ?? node.id,
        category: channels.indexOf(node.channel),
        symbolSize: Math.min(28, 12 + node.parents.length * 3),
      })),
      links: values.flatMap((node) => node.parents.map((parent) => ({ source: parent, target: node.id }))),
      force: { repulsion: 170, edgeLength: [65, 150], gravity: .08 },
      edgeSymbol: ["none", "arrow"],
      edgeSymbolSize: [0, 7],
      label: { show: true, position: "right", color: chartTheme.muted, fontSize: 10 },
      lineStyle: { color: "source", opacity: .46, width: 1.2, curveness: .08 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2 } },
    }],
  };
  return <EChart option={option} height={500} ariaLabel="Ориентированный граф наследий и их родителей" />;
}

interface TimelineApi {
  value: (dimension: number) => unknown;
  coord: (point: [number, number]) => [number, number];
  size: (point: [number, number]) => [number, number];
  style: (style?: Record<string, unknown>) => Record<string, unknown>;
}

interface TimelineParams {
  coordSys: { x: number; y: number; width: number; height: number };
}

function timelineRenderItem(params: TimelineParams, api: TimelineApi): Record<string, unknown> | null {
  const category = Number(api.value(0));
  const start = Number(api.value(1));
  const end = Number(api.value(2));
  const startCoord = api.coord([start, category]);
  const endCoord = api.coord([end, category]);
  const height = Math.min(16, Math.abs(api.size([0, 1])[1]) * .58);
  const x = Math.max(startCoord[0], params.coordSys.x);
  const right = Math.min(endCoord[0], params.coordSys.x + params.coordSys.width);
  if (right <= x) return null;
  return {
    type: "rect",
    shape: { x, y: startCoord[1] - height / 2, width: right - x, height, r: 3 },
    style: api.style({ stroke: chartTheme.dark, lineWidth: 1 }),
  };
}

const AGENT_GANTT_WINDOW = 300;

export function AgentGantt({ agents, currentTick = 0, page: serverPage }: {
  agents: AgentRecord[];
  currentTick?: number;
  page?: { offset: number; total?: number; hasMore: boolean };
}): React.JSX.Element {
  const chronological = useMemo(() => [...agents].sort((left, right) =>
    (left.born_tick ?? -1) - (right.born_tick ?? -1) ||
    (left.generation ?? -1) - (right.generation ?? -1) ||
    left.id.localeCompare(right.id)
  ), [agents]);
  const pageCount = Math.max(1, Math.ceil(chronological.length / AGENT_GANTT_WINDOW));
  const [page, setPage] = useState(pageCount - 1);
  useEffect(() => setPage(pageCount - 1), [agents.length, pageCount]);
  if (!agents.length) return <EmptyState title="Нет жизненных интервалов" detail="Read-model не вернул агентов." />;
  const safePage = Math.min(page, pageCount - 1);
  const windowStart = safePage * AGENT_GANTT_WINDOW;
  const windowEnd = Math.min(chronological.length, windowStart + AGENT_GANTT_WINDOW);
  const serverWindowStart = (serverPage?.offset ?? 0) + windowStart + 1;
  const serverWindowEnd = (serverPage?.offset ?? 0) + windowEnd;
  const sorted = [...chronological.slice(windowStart, windowEnd)].sort((left, right) =>
    (left.lineage_id ?? "").localeCompare(right.lineage_id ?? "") ||
    (left.generation ?? 0) - (right.generation ?? 0) ||
    left.id.localeCompare(right.id),
  );
  const maxTick = chronological.reduce(
    (maximum, agent) => Math.max(maximum, agent.died_tick ?? currentTick),
    Math.max(currentTick, 1),
  );
  // Show a readable slice by default; the rest stays reachable via the zoom slider.
  const GANTT_VISIBLE_ROWS = 28;
  const zoomEndIndex = Math.min(sorted.length - 1, GANTT_VISIBLE_ROWS - 1);
  const option = {
    animation: false,
    grid: { ...baseGrid, left: 120, top: 24, bottom: 40 },
    tooltip: {
      trigger: "item",
      renderMode: "richText",
      backgroundColor: "#102320",
      borderColor: "rgba(139,183,168,.35)",
      textStyle: { color: chartTheme.ink },
      formatter: (params: { data?: unknown[] }) => {
        const data = params.data ?? [];
        return `${String(data[3] ?? "agent")}\ntick ${String(data[1])} → ${String(data[2])}\n${String(data[4] ?? "alive")}`;
      },
    },
    xAxis: { ...baseAxis, type: "value", min: 0, max: maxTick, name: "tick" },
    yAxis: { ...baseAxis, type: "category", inverse: true, data: sorted.map((agent) => shortId(agent.id, 14)), axisLabel: { ...baseAxis.axisLabel, width: 100, overflow: "truncate" } },
    dataZoom: sorted.length > 18 ? [{ type: "inside", yAxisIndex: 0, startValue: 0, endValue: zoomEndIndex }, { type: "slider", yAxisIndex: 0, startValue: 0, endValue: zoomEndIndex, width: 8, right: 5, borderColor: "transparent", fillerColor: "rgba(79,180,194,.18)" }] : undefined,
    series: [{
      type: "custom",
      renderItem: timelineRenderItem,
      encode: { x: [1, 2], y: 0 },
      itemStyle: { color: chartTheme.blue },
      data: sorted.map((agent, index) => [index, agent.born_tick ?? 0, agent.died_tick ?? currentTick, agent.id, agent.cause ?? agent.status ?? "alive", agent.generation ?? 0]),
    }],
  } as unknown as EChartsOption;
  return (
    <div className="timeline-window">
      {chronological.length > AGENT_GANTT_WINDOW || (serverPage?.total ?? chronological.length) > chronological.length ? (
        <div className="timeline-window-controls" role="group" aria-label="Окно агентов на таймлайне">
          <p>
            Показаны server records <strong>{formatInteger(serverWindowStart)}–{formatInteger(serverWindowEnd)}</strong>; на этой странице загружено {formatInteger(chronological.length)}
            {serverPage?.total !== undefined ? <>; server total {formatInteger(serverPage.total)}</> : null}.
            Окна упорядочены по born_tick; одновременно рендерится не более {AGENT_GANTT_WINDOW} полос.
            {serverPage && (serverPage.hasMore || serverPage.offset > 0) ? " Остальные серверные страницы не загружены." : ""}
          </p>
          <div>
            <button type="button" className="button button-small button-ghost" disabled={safePage === 0} onClick={() => setPage((value) => Math.max(0, value - 1))}>← Ранее</button>
            <code>{safePage + 1} / {pageCount}</code>
            <button type="button" className="button button-small button-ghost" disabled={safePage >= pageCount - 1} onClick={() => setPage((value) => Math.min(pageCount - 1, value + 1))}>Позже →</button>
          </div>
        </div>
      ) : null}
      <EChart
        option={option}
        height={Math.min(650, Math.max(320, sorted.length * 28 + 100))}
        ariaLabel={`Жизненные интервалы server records ${serverWindowStart}–${serverWindowEnd}, ${chronological.length} загружено${serverPage?.total !== undefined ? `, server total ${serverPage.total}` : ""}, по оси committed tick`}
      />
    </div>
  );
}
