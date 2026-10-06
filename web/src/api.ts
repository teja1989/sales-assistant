import type { AppConfig, Metrics, ScenarioView, ServerEvent, SessionInfo } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    credentials: "same-origin",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new ApiError(res.status, (body as { message?: string }).message ?? `Request failed (${res.status})`);
  }
  return body as T;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  scenarios: () => request<{ scenarios: ScenarioView[] }>("/api/scenarios").then((r) => r.scenarios),
  handoff: (query: string, scenarioId?: string) =>
    request<{ matched: boolean; token: string; scenario: ScenarioView }>("/api/handoff", {
      method: "POST",
      body: JSON.stringify({ query, scenario_id: scenarioId }),
    }),
  createSession: (token: string, accessToken?: string) =>
    request<SessionInfo>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ handoff_token: token }),
      headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
    }),
  disconnect: (sessionId: string) =>
    request<{ disconnected: boolean }>(`/api/sessions/${encodeURIComponent(sessionId)}/disconnect`, { method: "POST" }),
  metrics: () => request<Metrics>("/api/metrics"),
  resetMetrics: () => request<{ reset: boolean }>("/api/metrics/reset", { method: "POST" }),
};

export type TurnBody =
  | { kickoff: true }
  | { message: string }
  | { confirmation: { action_id: string; approved: boolean } };

/** POST a chat turn and stream server-sent events back. */
export async function streamTurn(
  sessionId: string,
  body: TurnBody,
  onEvent: (event: ServerEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/turn`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
    credentials: "same-origin",
  });
  if (!res.ok || !res.body) {
    const payload = await res.json().catch(() => ({}));
    throw new ApiError(res.status, (payload as { message?: string }).message ?? `Request failed (${res.status})`);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const data = block
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) {
        try {
          onEvent(JSON.parse(data) as ServerEvent);
        } catch {
          // ignore malformed event
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
