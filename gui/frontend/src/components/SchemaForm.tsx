import type { JsonSchema } from "../api/types";
import { humanize } from "../lib/format";

type Path = Array<string | number>;

function resolveSchema(schema: JsonSchema, root: JsonSchema): JsonSchema {
  if (!schema.$ref) return schema;
  const parts = schema.$ref.replace(/^#\//, "").split("/").map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
  let value: unknown = root;
  for (const part of parts) {
    if (typeof value !== "object" || value === null || Array.isArray(value)) return schema;
    value = (value as Record<string, unknown>)[part];
  }
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? { ...(value as JsonSchema), ...schema, $ref: undefined }
    : schema;
}

function effectiveSchema(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const resolved = resolveSchema(schema, root);
  const branches = resolved.anyOf ?? resolved.oneOf;
  if (!branches?.length) return resolved;
  const branch = branches
    .map((item) => resolveSchema(item, root))
    .find((item) => !(
      item.type === "null"
      || item.const === null
      || (item.enum?.length && item.enum.every((value) => value === null))
    )) ?? branches[0];
  return branch ? { ...resolved, ...branch, anyOf: undefined, oneOf: undefined } : resolved;
}

function schemaAllowsNull(schemaInput: JsonSchema, root: JsonSchema): boolean {
  const schema = resolveSchema(schemaInput, root);
  if (schema.type === "null" || (Array.isArray(schema.type) && schema.type.includes("null"))) return true;
  if (schema.const === null) return true;
  if (schema.enum?.some((item) => item === null)) return true;
  return [...(schema.anyOf ?? []), ...(schema.oneOf ?? [])]
    .some((branch) => schemaAllowsNull(branch, root));
}

function schemaType(schema: JsonSchema): string {
  const type = Array.isArray(schema.type) ? schema.type.find((item) => item !== "null") : schema.type;
  if (type) return type;
  if (schema.properties) return "object";
  if (schema.items) return "array";
  const enumValue = schema.enum?.find((item) => item !== null);
  if (enumValue !== undefined) return typeof enumValue;
  return "string";
}

function readAt(root: unknown, path: Path): unknown {
  let current = root;
  for (const part of path) {
    if (Array.isArray(current) && typeof part === "number") current = current[part];
    else if (typeof current === "object" && current !== null && !Array.isArray(current)) {
      current = (current as Record<string, unknown>)[String(part)];
    } else return undefined;
  }
  return current;
}

function writeAt(root: Record<string, unknown>, path: Path, value: unknown): Record<string, unknown> {
  if (!path.length) return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : root;
  const clone = structuredClone(root);
  let current: Record<string, unknown> | unknown[] = clone;
  path.forEach((part, index) => {
    const last = index === path.length - 1;
    if (last) {
      if (Array.isArray(current) && typeof part === "number") current[part] = value;
      else if (!Array.isArray(current)) current[String(part)] = value;
      return;
    }
    const nextPart = path[index + 1];
    if (Array.isArray(current) && typeof part === "number") {
      const next = current[part];
      if (typeof next !== "object" || next === null) current[part] = typeof nextPart === "number" ? [] : {};
      current = current[part] as Record<string, unknown> | unknown[];
    } else if (!Array.isArray(current)) {
      const key = String(part);
      const next = current[key];
      if (typeof next !== "object" || next === null) current[key] = typeof nextPart === "number" ? [] : {};
      current = current[key] as Record<string, unknown> | unknown[];
    }
  });
  return clone;
}

function defaultFor(schemaInput: JsonSchema, root: JsonSchema, requireNonNull = false): unknown {
  const schema = effectiveSchema(schemaInput, root);
  if (schema.default !== undefined && (!requireNonNull || schema.default !== null)) return structuredClone(schema.default);
  if (schema.const !== undefined && (!requireNonNull || schema.const !== null)) return structuredClone(schema.const);
  const enumValue = requireNonNull ? schema.enum?.find((item) => item !== null) : schema.enum?.[0];
  if (enumValue !== undefined) return structuredClone(enumValue);
  switch (schemaType(schema)) {
    case "object":
      return Object.fromEntries(Object.entries(schema.properties ?? {}).map(([key, child]) => [key, defaultFor(child, root)]));
    case "array": return [];
    case "boolean": return false;
    case "integer":
    case "number": return schema.minimum ?? schema.exclusiveMinimum ?? 0;
    default: return "";
  }
}

function NullableReset({ path, onReset }: { path: Path; onReset: () => void }): React.JSX.Element {
  return (
    <button
      type="button"
      className="button button-small button-ghost nullable-reset"
      data-nullable-action="clear"
      data-nullable-path={path.join(".")}
      onClick={onReset}
    >Сбросить в null</button>
  );
}

function fieldId(path: Path): string {
  return `schema-${path.map(String).join("-") || "root"}`.replace(/[^A-Za-z0-9_-]/g, "-");
}

interface SchemaNodeProps {
  schema: JsonSchema;
  rootSchema: JsonSchema;
  dataRoot: Record<string, unknown>;
  path: Path;
  label?: string;
  required?: boolean;
  onRootChange: (value: Record<string, unknown>) => void;
  depth: number;
}

function SchemaNode({ schema: schemaInput, rootSchema, dataRoot, path, label, required, onRootChange, depth }: SchemaNodeProps): React.JSX.Element {
  const schema = effectiveSchema(schemaInput, rootSchema);
  const nullable = schemaAllowsNull(schemaInput, rootSchema);
  const type = schemaType(schema);
  const value = readAt(dataRoot, path);
  const id = fieldId(path);
  const title = schema.title ?? (label ? humanize(label) : "Configuration");
  const update = (next: unknown) => onRootChange(writeAt(dataRoot, path, next));

  if (nullable && (value === null || value === undefined)) {
    return (
      <div
        className={`nullable-field schema-field${type === "object" || type === "array" ? " schema-span-full" : ""}`}
        data-nullable-path={path.join(".")}
        data-nullable-state={value === null ? "null" : "unset"}
      >
        <div className="nullable-heading">
          <span>{title}{required ? <em aria-label="обязательное поле"> *</em> : null}</span>
          <code>{value === null ? "null" : "не задано"}</code>
        </div>
        {schema.description ? <small>{schema.description}</small> : null}
        <button
          type="button"
          className="button button-small button-ghost"
          data-nullable-action="set"
          data-nullable-path={path.join(".")}
          onClick={() => update(defaultFor(schemaInput, rootSchema, true))}
        >Задать значение</button>
      </div>
    );
  }

  if (type === "object") {
    const entries = Object.entries(schema.properties ?? {});
    const requiredKeys = new Set(schema.required ?? []);
    return (
      <fieldset className={`schema-group schema-depth-${Math.min(depth, 3)}`}>
        {path.length ? (
          <legend>
            <span>{title}</span>
            {schema.description ? <small>{schema.description}</small> : null}
          </legend>
        ) : null}
        {nullable ? <NullableReset path={path} onReset={() => update(null)} /> : null}
        <div className="schema-grid">
          {entries.map(([key, child]) => (
            <SchemaNode
              key={key}
              schema={child}
              rootSchema={rootSchema}
              dataRoot={dataRoot}
              path={[...path, key]}
              label={key}
              required={requiredKeys.has(key)}
              onRootChange={onRootChange}
              depth={depth + 1}
            />
          ))}
        </div>
      </fieldset>
    );
  }

  if (type === "array") {
    const values = Array.isArray(value) ? value : [];
    const itemSchema = schema.items ?? {};
    const maxItems = schema.maxItems ?? 128;
    return (
      <fieldset className="schema-array schema-span-full">
        <legend>
          <span>{title}{required ? <em aria-label="обязательное поле"> *</em> : null}</span>
          {schema.description ? <small>{schema.description}</small> : null}
        </legend>
        {nullable ? <NullableReset path={path} onReset={() => update(null)} /> : null}
        <div className="array-items">
          {values.map((_, index) => (
            <div className="array-item" key={`${id}-${index}`}>
              <div className="array-item-index">#{index + 1}</div>
              <SchemaNode
                schema={itemSchema}
                rootSchema={rootSchema}
                dataRoot={dataRoot}
                path={[...path, index]}
                label={`${label ?? "item"} ${index + 1}`}
                required
                onRootChange={onRootChange}
                depth={depth + 1}
              />
              <button
                type="button"
                className="icon-button danger"
                aria-label={`Удалить элемент ${index + 1}`}
                onClick={() => update(values.filter((__, itemIndex) => itemIndex !== index))}
              >×</button>
            </div>
          ))}
          {!values.length ? <p className="muted compact">Список пуст.</p> : null}
        </div>
        <button
          type="button"
          className="button button-small button-ghost"
          disabled={values.length >= maxItems}
          onClick={() => update([...values, defaultFor(itemSchema, rootSchema)])}
        >+ Добавить</button>
      </fieldset>
    );
  }

  if (type === "boolean") {
    return (
      <div className="schema-field nullable-active">
        <label className="boolean-field">
          <input type="checkbox" checked={Boolean(value)} onChange={(event) => update(event.target.checked)} />
          <span className="toggle" aria-hidden="true" />
          <span>
            <strong>{title}{required ? <em aria-label="обязательное поле"> *</em> : null}</strong>
            {schema.description ? <small>{schema.description}</small> : null}
          </span>
        </label>
        {nullable ? <NullableReset path={path} onReset={() => update(null)} /> : null}
      </div>
    );
  }

  const metadata = [
    schema.minimum !== undefined ? `min ${schema.minimum}` : undefined,
    schema.maximum !== undefined ? `max ${schema.maximum}` : undefined,
    schema.pattern ? `pattern ${schema.pattern}` : undefined,
  ].filter(Boolean).join(" · ");

  const enumValues = schema.enum?.filter((item) => item !== null);
  if (enumValues?.length) {
    return (
      <div className="schema-field nullable-active">
        <label className="field" htmlFor={id}>
          <span>{title}{required ? <em aria-label="обязательное поле"> *</em> : null}</span>
          <select id={id} value={String(value ?? "")} onChange={(event) => {
            const selected = enumValues.find((item) => String(item) === event.target.value);
            update(selected ?? event.target.value);
          }}>
            {enumValues.map((item, index) => <option key={`${String(item)}-${index}`} value={String(item)}>{String(item)}</option>)}
          </select>
          {schema.description ? <small>{schema.description}</small> : null}
        </label>
        {nullable ? <NullableReset path={path} onReset={() => update(null)} /> : null}
      </div>
    );
  }

  const numeric = type === "integer" || type === "number";
  return (
    <div className="schema-field nullable-active">
      <label className="field" htmlFor={id}>
        <span>{title}{required ? <em aria-label="обязательное поле"> *</em> : null}</span>
        <input
          id={id}
          type={numeric ? "number" : "text"}
          value={typeof value === "string" || typeof value === "number" ? value : ""}
          min={schema.minimum}
          max={schema.maximum}
          step={type === "integer" ? 1 : numeric ? "any" : undefined}
          required={required}
          pattern={schema.pattern}
          onChange={(event) => {
            if (event.target.value === "" && nullable) update(null);
            else if (!numeric) update(event.target.value);
            else if (event.target.value === "") update("");
            else update(type === "integer" ? Number.parseInt(event.target.value, 10) : Number(event.target.value));
          }}
        />
        {schema.description ? <small>{schema.description}</small> : metadata ? <small className="mono">{metadata}</small> : null}
      </label>
      {nullable ? <NullableReset path={path} onReset={() => update(null)} /> : null}
    </div>
  );
}

export function buildSchemaDefaults(schema: JsonSchema): Record<string, unknown> {
  const defaults = defaultFor(schema, schema);
  return typeof defaults === "object" && defaults !== null && !Array.isArray(defaults)
    ? defaults as Record<string, unknown>
    : {};
}

export function SchemaForm({ schema, value, onChange }: {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (value: Record<string, unknown>) => void;
}): React.JSX.Element {
  return (
    <form className="schema-form" onSubmit={(event) => event.preventDefault()}>
      <SchemaNode
        schema={schema}
        rootSchema={schema}
        dataRoot={value}
        path={[]}
        onRootChange={onChange}
        depth={0}
      />
    </form>
  );
}
