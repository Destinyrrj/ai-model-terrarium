import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { JsonSchema } from "../api/types";
import { SchemaForm } from "./SchemaForm";

// Exact nullable portions of RunConfig.model_json_schema(), including its refs.
const runConfigSchema: JsonSchema = {
  $defs: {
    RuntimeConfig: {
      additionalProperties: false,
      properties: {
        executable_sha256: {
          anyOf: [
            { pattern: "^[a-f0-9]{64}$", type: "string" },
            { type: "null" },
          ],
          default: null,
          title: "Executable Sha256",
        },
        sandbox: { $ref: "#/$defs/SandboxConfig" },
      },
      required: ["sandbox"],
      title: "RuntimeConfig",
      type: "object",
    },
    SandboxConfig: {
      additionalProperties: false,
      properties: {
        egress_proxy: {
          anyOf: [
            { maxLength: 2048, type: "string" },
            { type: "null" },
          ],
          default: null,
          title: "Egress Proxy",
        },
        bwrap_sha256: {
          anyOf: [
            { pattern: "^[a-f0-9]{64}$", type: "string" },
            { type: "null" },
          ],
          default: null,
          title: "Bwrap Sha256",
        },
      },
      title: "SandboxConfig",
      type: "object",
    },
  },
  properties: { runtime: { $ref: "#/$defs/RuntimeConfig" } },
  required: ["runtime"],
  type: "object",
};

const roots: Root[] = [];

function mount(initial: Record<string, unknown>): {
  container: HTMLDivElement;
  current: () => Record<string, unknown>;
} {
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  roots.push(root);
  let latest = initial;

  function Harness(): React.JSX.Element {
    const [value, setValue] = useState(initial);
    latest = value;
    return <SchemaForm schema={runConfigSchema} value={value} onChange={setValue} />;
  }

  act(() => root.render(<Harness />));
  return { container, current: () => latest };
}

function click(element: Element | null): void {
  expect(element).not.toBeNull();
  act(() => (element as HTMLElement).click());
}

function inputValue(input: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  expect(setter).toBeTypeOf("function");
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

afterEach(() => {
  for (const root of roots.splice(0)) act(() => root.unmount());
  document.body.replaceChildren();
});

describe("SchemaForm nullable controls", () => {
  it("renders explicit null and unset states from the real Pydantic schema", () => {
    const { container } = mount({
      runtime: {
        executable_sha256: null,
        sandbox: { egress_proxy: null },
      },
    });

    expect(container.querySelector('[data-nullable-path="runtime.executable_sha256"]')?.getAttribute("data-nullable-state")).toBe("null");
    expect(container.querySelector('[data-nullable-path="runtime.sandbox.egress_proxy"]')?.getAttribute("data-nullable-state")).toBe("null");
    expect(container.querySelector('[data-nullable-path="runtime.sandbox.bwrap_sha256"]')?.getAttribute("data-nullable-state")).toBe("unset");
    expect(container.querySelectorAll('[data-nullable-action="set"]')).toHaveLength(3);
    expect(container.textContent).toContain("Executable Sha256");
    expect(container.textContent).toContain("Egress Proxy");
    expect(container.textContent).toContain("Bwrap Sha256");
  });

  it("writes null when a nullable text input is cleared and supports explicit reset", () => {
    const { container, current } = mount({
      runtime: {
        executable_sha256: null,
        sandbox: { egress_proxy: null, bwrap_sha256: null },
      },
    });

    click(container.querySelector('[data-nullable-action="set"][data-nullable-path="runtime.executable_sha256"]'));
    const digestInput = container.querySelector<HTMLInputElement>("#schema-runtime-executable_sha256");
    expect(digestInput).not.toBeNull();
    inputValue(digestInput as HTMLInputElement, "a".repeat(64));
    expect((current().runtime as Record<string, unknown>).executable_sha256).toBe("a".repeat(64));

    inputValue(digestInput as HTMLInputElement, "");
    expect((current().runtime as Record<string, unknown>).executable_sha256).toBeNull();
    expect(container.querySelector("#schema-runtime-executable_sha256")).toBeNull();

    click(container.querySelector('[data-nullable-action="set"][data-nullable-path="runtime.sandbox.egress_proxy"]'));
    const proxyInput = container.querySelector<HTMLInputElement>("#schema-runtime-sandbox-egress_proxy");
    expect(proxyInput).not.toBeNull();
    inputValue(proxyInput as HTMLInputElement, "https://proxy.invalid");
    click(container.querySelector('[data-nullable-action="clear"][data-nullable-path="runtime.sandbox.egress_proxy"]'));
    const sandbox = ((current().runtime as Record<string, unknown>).sandbox as Record<string, unknown>);
    expect(sandbox.egress_proxy).toBeNull();
  });
});
