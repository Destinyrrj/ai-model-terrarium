import type { WorldAgent, WorldLocation, WorldSnapshot } from "../../api/types";
import { EmptyState, SanitizedText } from "../Primitives";

interface Point { x: number; y: number }

function extent(values: number[]): [number, number] {
  if (!values.length) return [0, 1];
  const low = Math.min(...values);
  const high = Math.max(...values);
  return low === high ? [low - .5, high + .5] : [low, high];
}

function mapRange(value: number, [low, high]: [number, number], outputLow: number, outputHigh: number): number {
  return outputLow + ((value - low) / (high - low)) * (outputHigh - outputLow);
}

function locationLayout(world: WorldSnapshot, agents: WorldAgent[]): Map<string, Point> {
  const result = new Map<string, Point>();
  const derivedIds = [...new Set(agents.map((agent) => agent.location).filter((item): item is string => Boolean(item)))];
  const locations: WorldLocation[] = world.locations.length
    ? world.locations
    : derivedIds.map((id) => ({ id }));
  const explicit = locations.filter((location) => location.x !== undefined && location.y !== undefined);
  const xExtent = extent(explicit.flatMap((location) => location.x === undefined ? [] : [location.x]));
  const yExtent = extent(explicit.flatMap((location) => location.y === undefined ? [] : [location.y]));
  locations.forEach((location, index) => {
    if (location.x !== undefined && location.y !== undefined) {
      result.set(location.id, { x: mapRange(location.x, xExtent, 90, 710), y: mapRange(location.y, yExtent, 70, 345) });
    } else {
      const angle = locations.length === 1 ? -Math.PI / 2 : (index / locations.length) * Math.PI * 2 - Math.PI / 2;
      result.set(location.id, { x: 400 + Math.cos(angle) * 255, y: 210 + Math.sin(angle) * 135 });
    }
  });
  return result;
}

function agentPoint(agent: WorldAgent, locationPoints: Map<string, Point>, index: number): Point {
  const location = agent.location ? locationPoints.get(agent.location) : undefined;
  const base = location ?? { x: 400, y: 210 };
  const angle = (index * 2.399963229728653) % (Math.PI * 2);
  const radius = 13 + (index % 3) * 7;
  return { x: base.x + Math.cos(angle) * radius, y: base.y + Math.sin(angle) * radius };
}

export function WorldMap({ world }: { world: WorldSnapshot }): React.JSX.Element {
  const liveAgents = world.agents.filter((agent) => agent.alive !== false);
  const hiddenDeadAgents = world.agents.length - liveAgents.length;
  const points = locationLayout(world, liveAgents);
  if (!points.size && !liveAgents.length) return <EmptyState title="World checkpoint недоступен" detail="Карта строится только из committed state_checkpoint." />;
  const locations: WorldLocation[] = world.locations.length
    ? world.locations
    : [...points.keys()].map((id): WorldLocation => ({ id }));
  const maxFood = Math.max(1, ...locations.map((location) => location.food ?? 0));

  return (
    <div className="world-map-wrap">
      <svg className="world-map" viewBox="0 0 800 420" role="img" aria-labelledby="world-title world-desc">
        <title id="world-title">Карта мира на committed tick {world.tick ?? "unknown"}</title>
        <desc id="world-desc">Локации, связи, запасы пищи и живые агенты из checkpoint.</desc>
        <defs>
          <pattern id="world-grid" width="25" height="25" patternUnits="userSpaceOnUse">
            <path d="M 25 0 L 0 0 0 25" fill="none" stroke="rgba(129,165,151,.08)" strokeWidth="1" />
          </pattern>
        </defs>
        <rect x="0" y="0" width="800" height="420" rx="12" fill="url(#world-grid)" />
        <g className="world-edges">
          {world.edges.map((edge, index) => {
            const source = points.get(edge.source);
            const target = points.get(edge.target);
            return source && target ? <line key={`${edge.source}-${edge.target}-${index}`} x1={source.x} y1={source.y} x2={target.x} y2={target.y} /> : null;
          })}
        </g>
        <g className="world-locations">
          {locations.map((location) => {
            const point = points.get(location.id);
            if (!point) return null;
            const foodRatio = Math.max(0, Math.min(1, (location.food ?? 0) / maxFood));
            return (
              <g key={location.id} transform={`translate(${point.x} ${point.y})`}>
                <circle r="32" className="location-halo" />
                <circle r="20" className="location-node" />
                <circle r={foodRatio === 0 ? 2 : Math.max(3, 15 * Math.sqrt(foodRatio))} className="food-node" />
                <text y="45" textAnchor="middle">{location.id}</text>
                {location.food !== undefined ? <text y="-31" textAnchor="middle" className="food-label">food {location.food}</text> : null}
                <title>{`${location.id}${location.kind ? ` · ${location.kind}` : ""}${location.food !== undefined ? ` · food ${location.food}` : ""}`}</title>
              </g>
            );
          })}
        </g>
        <g className="world-agents">
          {liveAgents.map((agent, index) => {
            const point = agentPoint(agent, points, index);
            const unhealthy = agent.health !== undefined && agent.health <= 35;
            return (
              <g key={agent.id} transform={`translate(${point.x} ${point.y})`}>
                <circle r="7" className={unhealthy ? "agent-node unhealthy" : "agent-node"} />
                <text x="10" y="4">{agent.id.length > 12 ? `${agent.id.slice(0, 12)}…` : agent.id}</text>
                <title>{`${agent.id} · ${agent.location ?? "unknown location"} · health ${agent.health ?? "?"} · hunger ${agent.hunger ?? "?"}`}</title>
              </g>
            );
          })}
        </g>
      </svg>
      <div className="world-legend">
        <span><i className="legend-location" /> location</span>
        <span><i className="legend-food" /> relative food</span>
        <span><i className="legend-agent" /> agent</span>
        <span><i className="legend-agent unhealthy" /> health ≤ 35</span>
        {hiddenDeadAgents ? <span>{hiddenDeadAgents} dead historical agent{hiddenDeadAgents === 1 ? "" : "s"} hidden</span> : null}
        <span className="world-tick"><SanitizedText value={`checkpoint tick ${world.tick ?? "—"}`} /></span>
      </div>
    </div>
  );
}
