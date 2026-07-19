import { asRecord, asString } from "../api/normalize";

function patchNested(root: Record<string, unknown>, path: string[], patch: Record<string, unknown>): Record<string, unknown> {
  const clone = structuredClone(root);
  let current = clone;
  for (const key of path) {
    const child = asRecord(current[key]);
    current[key] = { ...child };
    current = current[key] as Record<string, unknown>;
  }
  Object.assign(current, patch);
  return clone;
}

export function SandboxSafetyMatrix({ value, onChange }: {
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}): React.JSX.Element {
  const runtime = asRecord(value.runtime);
  const sandbox = asRecord(runtime.sandbox);
  const adapter = asString(runtime.adapter) ?? "mock";
  const backend = asString(sandbox.backend) ?? "mock";
  const network = asString(sandbox.network) ?? "none";
  const unsafe = Boolean(sandbox.acknowledge_unsafe_host_execution);
  const boundary = Boolean(sandbox.external_egress_enforced);
  const hazardous = adapter === "claude-code" || backend === "process" || network === "inherit";

  const updateSandbox = (patch: Record<string, unknown>) => onChange(patchNested(value, ["runtime", "sandbox"], patch));

  return (
    <section className="safety-matrix" aria-labelledby="safety-title">
      <header>
        <div>
          <span className="eyebrow">Safety boundary</span>
          <h3 id="safety-title">Матрица исполнения</h3>
        </div>
        <span className={`risk-badge ${hazardous ? "risk-high" : backend === "bubblewrap" ? "risk-medium" : "risk-low"}`}>
          {hazardous ? "host boundary" : backend === "bubblewrap" ? "isolated" : "deterministic mock"}
        </span>
      </header>
      {adapter === "claude-code" ? (
        <div className="notice notice-warning" role="alert">
          <strong>Claude Code исполняется на хосте и требует сеть.</strong>
          <span>OAuth-адаптер допустим только с backend=process, network=inherit и явным подтверждением риска.</span>
        </div>
      ) : null}
      <div className="matrix-grid">
        <div><span>Adapter</span><strong>{adapter}</strong></div>
        <div><span>Sandbox</span><strong>{backend}</strong></div>
        <div><span>Network</span><strong>{network}</strong></div>
        <div><span>Egress boundary</span><strong>{boundary ? "external / enforced" : "not asserted"}</strong></div>
      </div>
      <div className="acknowledgements">
        <label className={hazardous && !unsafe ? "ack-required" : undefined}>
          <input
            type="checkbox"
            checked={unsafe}
            onChange={(event) => updateSandbox({ acknowledge_unsafe_host_execution: event.target.checked })}
          />
          <span>
            Я подтверждаю небезопасное исполнение на хосте или наследуемую сеть
            <small>Обязательно для process и network=inherit.</small>
          </span>
        </label>
        <label className={network === "provider-proxy" && !boundary ? "ack-required" : undefined}>
          <input
            type="checkbox"
            checked={boundary}
            onChange={(event) => updateSandbox({ external_egress_enforced: event.target.checked })}
          />
          <span>
            Внешняя граница egress действительно принудительно применяется
            <small>Устанавливайте только для проверенного provider-proxy.</small>
          </span>
        </label>
      </div>
    </section>
  );
}
