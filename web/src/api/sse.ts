import { ApiClientError, getApiBase } from './client';
import type { ApiError, SearchRequest, SearchResponse, TraceStep } from './types';

export interface SearchStreamCallbacks {
  onSpan: (step: TraceStep) => void;
  onFinal: (response: SearchResponse) => void;
  onError: (err: ApiError | Error) => void;
}

/**
 * Потоковый клиент POST /v1/search/stream (CONTRACTS.md B.9).
 * Надёжно собирает SSE-фреймы поверх chunked ReadableStream, обрабатывая границу чанков и CRLF.
 */
export async function streamSearch(
  payload: SearchRequest,
  callbacks: SearchStreamCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${getApiBase()}/v1/search/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok) {
    let msg = `Ошибка поиска (HTTP ${resp.status})`;
    let code = `HTTP_${resp.status}`;
    try {
      const errJson = (await resp.json()) as { detail?: ApiError | string } & Partial<ApiError>;
      const detail = typeof errJson.detail === 'object' && errJson.detail !== null ? errJson.detail : errJson;
      msg = (typeof errJson.detail === 'string' ? errJson.detail : detail.message) || msg;
      code = detail.error_code ?? code;
    } catch {
      // ignore non-json error body
    }
    throw new ApiClientError(resp.status, msg, code, resp.headers.get('X-Trace-Id'));
  }

  if (!resp.body) {
    throw new Error('Сервер не вернул поток SSE (resp.body пуст)');
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  let receivedFinal = false;

  const processFrame = (rawFrame: string): void => {
    const lines = rawFrame.split(/\r?\n/);
    let eventName = 'message';
    const dataLines: string[] = [];

    for (const line of lines) {
      if (!line || line.startsWith(':')) continue;
      if (line.startsWith('event:')) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart());
      }
    }

    if (dataLines.length === 0) return;
    const rawData = dataLines.join('\n');

    try {
      const parsed = JSON.parse(rawData) as unknown;
      if (eventName === 'span') {
        callbacks.onSpan(parsed as TraceStep);
      } else if (eventName === 'final') {
        receivedFinal = true;
        callbacks.onFinal(parsed as SearchResponse);
      } else if (eventName === 'error') {
        callbacks.onError(parsed as ApiError);
      }
    } catch (e) {
      callbacks.onError(
        new Error(`Не удалось разобрать SSE-событие "${eventName}": ${(e as Error).message}`),
      );
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      // Граница SSE-события — пустая строка (\n\n или \r\n\r\n)
      const parts = buffer.split(/\r?\n\r?\n/);
      buffer = parts.pop() ?? '';

      for (const frame of parts) {
        if (frame.trim()) {
          processFrame(frame);
        }
      }
    }

    const tail = (buffer + decoder.decode()).trim();
    if (tail) {
      processFrame(tail);
    }

    if (!receivedFinal && !signal?.aborted) {
      callbacks.onError(new Error('SSE-поток завершился без события final'));
    }
  } finally {
    reader.releaseLock();
  }
}