import { dump as dumpYaml, load as loadYaml } from "js-yaml";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { isRecord } from "../api/normalize";
import type { JsonSchema, ValidationResult } from "../api/types";
import { ErrorNotice, LoadingBlock, SanitizedText } from "../components/Primitives";
import { SandboxSafetyMatrix } from "../components/SandboxSafetyMatrix";
import { buildSchemaDefaults, SchemaForm } from "../components/SchemaForm";
import { useAsync, useDocumentTitle } from "../lib/hooks";
import { navigate } from "../router";

function yamlFromValue(value: Record<string, unknown>): string {
  return dumpYaml(value, { indent: 2, lineWidth: 110, noRefs: true, sortKeys: false });
}

function parseYamlDocument(source: string): Record<string, unknown> {
  const parsed = loadYaml(source, { json: false });
  if (!isRecord(parsed)) throw new Error("Корень YAML должен быть mapping-объектом.");
  return parsed;
}

function ValidationPanel({ result }: { result: ValidationResult }): React.JSX.Element {
  if (result.valid) return <div className="notice notice-success"><strong>Конфигурация валидна.</strong><span>Pydantic принял все поля и safety-инварианты.</span></div>;
  return (
    <div className="validation-errors" role="alert">
      <strong>Найдены ошибки конфигурации</strong>
      <ul>
        {result.issues.map((issue, index) => (
          <li key={`${issue.loc.join(".")}-${index}`}>
            <code>{issue.loc.length ? issue.loc.join(".") : "root"}</code>
            <SanitizedText value={issue.message} />
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ConfigsPage({ initialName }: { initialName?: string }): React.JSX.Element {
  useDocumentTitle("Terrarium · Конфигурации");
  const schemaState = useAsync(() => api.configSchema(), []);
  const configsState = useAsync(() => api.configs(), []);
  const [selected, setSelected] = useState(initialName ?? "");
  const [name, setName] = useState(initialName ?? "");
  const [data, setData] = useState<Record<string, unknown>>({});
  const [yaml, setYaml] = useState("");
  const [parseError, setParseError] = useState<Error>();
  const [loadError, setLoadError] = useState<Error>();
  const [validation, setValidation] = useState<ValidationResult>();
  const [busy, setBusy] = useState<"save" | "validate" | "delete" | null>(null);
  const [dirty, setDirty] = useState(false);

  const schema = schemaState.data;
  const configNames = useMemo(() => configsState.data?.map((config) => config.name) ?? [], [configsState.data]);

  useEffect(() => {
    if (!selected && configNames[0]) setSelected(configNames[0]);
  }, [configNames, selected]);

  useEffect(() => {
    if (!selected) return;
    let active = true;
    setLoadError(undefined);
    void api.config(selected).then((config) => {
      if (!active) return;
      try {
        const value = config.data ?? parseYamlDocument(config.yaml);
        setData(value);
        setYaml(config.yaml || yamlFromValue(value));
        setName(selected);
        setDirty(false);
        setParseError(undefined);
        setValidation(undefined);
      } catch (reason) {
        setLoadError(reason instanceof Error ? reason : new Error(String(reason)));
      }
    }).catch((reason: unknown) => {
      if (active) setLoadError(reason instanceof Error ? reason : new Error(String(reason)));
    });
    return () => { active = false; };
  }, [selected]);

  const updateFromForm = (value: Record<string, unknown>) => {
    setData(value);
    setYaml(yamlFromValue(value));
    setDirty(true);
    setParseError(undefined);
    setValidation(undefined);
  };

  const updateFromYaml = (source: string) => {
    setYaml(source);
    setDirty(true);
    setValidation(undefined);
    try {
      setData(parseYamlDocument(source));
      setParseError(undefined);
    } catch (reason) {
      setParseError(reason instanceof Error ? reason : new Error(String(reason)));
    }
  };

  const createConfig = () => {
    const defaults = schema ? buildSchemaDefaults(schema) : {};
    setSelected("");
    setName("new-config");
    setData(defaults);
    setYaml(yamlFromValue(defaults));
    setDirty(true);
    setParseError(undefined);
    setValidation(undefined);
  };

  const validate = async (): Promise<ValidationResult | undefined> => {
    if (parseError) return undefined;
    setBusy("validate");
    try {
      const result = await api.validateConfig(yaml);
      setValidation(result);
      return result;
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason : new Error(String(reason)));
      return undefined;
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!name.trim() || parseError) return;
    setBusy("save");
    setLoadError(undefined);
    try {
      const result = await api.validateConfig(yaml);
      setValidation(result);
      if (!result.valid) return;
      await api.saveConfig(name.trim(), yaml);
      setSelected(name.trim());
      setDirty(false);
      await configsState.refresh();
      navigate(`/configs/${encodeURIComponent(name.trim())}`);
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason : new Error(String(reason)));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (!selected || !window.confirm(`Удалить конфигурацию «${selected}»?`)) return;
    setBusy("delete");
    try {
      await api.deleteConfig(selected);
      setSelected("");
      setName("");
      setData({});
      setYaml("");
      await configsState.refresh();
      navigate("/configs");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason : new Error(String(reason)));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page config-page">
      <header className="page-header">
        <div><span className="eyebrow">Versioned experiment inputs</span><h1>Конфигурации</h1><p>Форма генерируется из реальной JSON Schema. YAML остаётся каноническим редактируемым представлением.</p></div>
        <button type="button" className="button button-primary" onClick={createConfig}>+ Новая конфигурация</button>
      </header>

      <div className="config-layout">
        <aside className="config-sidebar">
          <span className="sidebar-title">Файлы</span>
          {configsState.loading && !configsState.data ? <LoadingBlock label="Читаем configs-dir" /> : null}
          {configsState.error ? <ErrorNotice error={configsState.error} retry={() => void configsState.refresh()} /> : null}
          <div className="config-list">
            {configsState.data?.map((config) => (
              <button
                type="button"
                key={config.name}
                className={selected === config.name ? "active" : undefined}
                onClick={() => {
                  if (!dirty || window.confirm("Отменить несохранённые изменения?")) {
                    setSelected(config.name);
                    navigate(`/configs/${encodeURIComponent(config.name)}`);
                  }
                }}
              >
                <span className="file-icon" aria-hidden="true">Y</span>
                <span><strong>{config.name}</strong><small>{config.size ? `${config.size} bytes` : "YAML"}</small></span>
              </button>
            ))}
          </div>
        </aside>

        <section className="config-workbench">
          <div className="workbench-toolbar">
            <label className="field compact-field"><span>Имя файла</span><input value={name} onChange={(event) => { setName(event.target.value); setDirty(true); }} pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,95}" /></label>
            <span className={dirty ? "dirty-indicator active" : "dirty-indicator"}>{dirty ? "● изменено" : "✓ сохранено"}</span>
            <div className="toolbar-actions">
              {selected ? <button type="button" className="button button-danger button-small" disabled={busy !== null} onClick={() => void remove()}>Удалить</button> : null}
              <button type="button" className="button button-ghost" disabled={busy !== null || Boolean(parseError)} onClick={() => void validate()}>{busy === "validate" ? "Проверяем…" : "Проверить"}</button>
              <button type="button" className="button button-primary" disabled={busy !== null || Boolean(parseError) || !name.trim()} onClick={() => void save()}>{busy === "save" ? "Сохраняем…" : "Сохранить"}</button>
            </div>
          </div>

          {loadError ? <ErrorNotice error={loadError} /> : null}
          {schemaState.loading ? <LoadingBlock label="Загружаем JSON Schema" /> : null}
          {schemaState.error ? <ErrorNotice error={schemaState.error} retry={() => void schemaState.refresh()} /> : null}

          {schema ? (
            <>
              <SandboxSafetyMatrix value={data} onChange={updateFromForm} />
              <div className="editor-split">
                <div className="form-editor">
                  <div className="editor-heading"><div><span className="eyebrow">Structured</span><h2>Параметры</h2></div><span className="sync-state">↔ YAML</span></div>
                  <SchemaForm schema={schema as JsonSchema} value={data} onChange={updateFromForm} />
                </div>
                <div className="yaml-editor">
                  <div className="editor-heading"><div><span className="eyebrow">Source</span><h2>YAML</h2></div><span className={parseError ? "syntax-state invalid" : "syntax-state"}>{parseError ? "syntax error" : "parsed"}</span></div>
                  <textarea aria-label="YAML конфигурации" spellCheck={false} value={yaml} onChange={(event) => updateFromYaml(event.target.value)} />
                  {parseError ? <div className="inline-error"><SanitizedText value={parseError.message} /></div> : null}
                </div>
              </div>
              {validation ? <ValidationPanel result={validation} /> : null}
            </>
          ) : null}
        </section>
      </div>
    </div>
  );
}
