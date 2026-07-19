import { useCallback, useEffect, useRef, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  refresh: () => Promise<void>;
}

export function useAsync<T>(loader: () => Promise<T>, dependencies: readonly unknown[]): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const generation = useRef(0);

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true);
    setError(undefined);
    try {
      const value = await loader();
      if (current === generation.current) setData(value);
    } catch (reason) {
      if (current === generation.current) {
        setError(reason instanceof Error ? reason : new Error(String(reason)));
      }
    } finally {
      if (current === generation.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, dependencies);

  useEffect(() => {
    void refresh();
    return () => {
      generation.current += 1;
    };
  }, [refresh]);

  return { data, error, loading, refresh };
}

export function useDebouncedCallback(callback: () => void, waitMs: number): () => void {
  const callbackRef = useRef(callback);
  const timeoutRef = useRef<number | undefined>(undefined);
  callbackRef.current = callback;

  useEffect(() => () => window.clearTimeout(timeoutRef.current), []);
  return useCallback(() => {
    window.clearTimeout(timeoutRef.current);
    timeoutRef.current = window.setTimeout(() => callbackRef.current(), waitMs);
  }, [waitMs]);
}

export function useDocumentTitle(title: string): void {
  useEffect(() => {
    const previous = document.title;
    document.title = title;
    return () => {
      document.title = previous;
    };
  }, [title]);
}
