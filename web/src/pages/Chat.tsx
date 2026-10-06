import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { ApiError, api, streamTurn, type TurnBody } from "../api";
import { Link } from "../router";
import { Avatar, FRIENDLY_TOOL } from "../components/Avatar";
import { BrandMark } from "../components/BrandMark";
import { ConfirmCard } from "../components/ConfirmCard";
import { SourceBadge, ToolCard } from "../components/ToolCard";
import { Markdown } from "../markdown";
import type { AppConfig, ChatItem, ServerEvent, SessionInfo } from "../types";

let seq = 0;
const uid = () => `i${++seq}`;

type Action = { type: "event"; event: ServerEvent } | { type: "user"; text: string } | { type: "notice"; text: string; tone: "info" | "warn" | "error" };

function reducer(items: ChatItem[], action: Action): ChatItem[] {
  if (action.type === "user") return [...items, { kind: "user", id: uid(), text: action.text }];
  if (action.type === "notice") return [...items, { kind: "notice", id: uid(), text: action.text, tone: action.tone }];
  const e = action.event;
  switch (e.type) {
    case "text": {
      const idx = items.findIndex((i) => i.kind === "bot" && i.id === e.segment);
      if (idx === -1) return [...items, { kind: "bot", id: e.segment, text: e.delta, streaming: true }];
      const next = items.slice();
      const bot = next[idx] as Extract<ChatItem, { kind: "bot" }>;
      next[idx] = { ...bot, text: bot.text + e.delta };
      return next;
    }
    case "segment_end": {
      const idx = items.findIndex((i) => i.kind === "bot" && i.id === e.segment);
      if (idx === -1) return [...items, { kind: "bot", id: e.segment, text: e.text, streaming: false }];
      const next = items.slice();
      next[idx] = { kind: "bot", id: e.segment, text: e.text, streaming: false };
      return next;
    }
    case "tool_start":
      return [...items, { kind: "tool", id: e.call_id, tool: e.tool, title: e.title, source: e.source, status: "running" }];
    case "tool_result": {
      const updated: ChatItem = {
        kind: "tool",
        id: e.call_id,
        tool: e.tool,
        title: e.title,
        source: e.source,
        status: e.is_error ? "error" : "done",
        data: e.data,
        latency: e.latency_ms,
        fallback: e.fallback,
      };
      const idx = items.findIndex((i) => i.kind === "tool" && i.id === e.call_id);
      if (idx === -1) return [...items, updated];
      const next = items.slice();
      next[idx] = updated;
      return next;
    }
    case "confirm_required":
      return [
        ...items,
        { kind: "confirm", id: e.action_id, tool: e.tool, title: e.title, summary: e.summary, details: e.details, state: "pending" },
      ];
    case "action_resolved":
      return items.map((i) =>
        i.kind === "confirm" && i.id === e.action_id
          ? { ...i, state: e.superseded ? "superseded" : e.approved ? "approved" : "declined" }
          : i,
      );
    case "guardrail":
      return [...items, { kind: "notice", id: uid(), text: `Guardrail: ${e.message}`, tone: "warn" }];
    case "notice":
      return [...items, { kind: "notice", id: uid(), text: e.message, tone: "info" }];
    case "error":
      return [...items, { kind: "notice", id: uid(), text: e.message, tone: "error" }];
    default:
      return items;
  }
}

export function Chat({ config }: { config: AppConfig | null }) {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [fatal, setFatal] = useState<string | null>(null);
  const [items, dispatch] = useReducer(reducer, []);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [showTrace, setShowTrace] = useState(false);
  const started = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);

  const send = useCallback(
    async (sessionId: string, body: TurnBody) => {
      setBusy(true);
      try {
        await streamTurn(sessionId, body, (event) => dispatch({ type: "event", event }));
      } catch (err) {
        const msg = err instanceof ApiError ? err.message : "Connection lost. Please try again.";
        dispatch({ type: "notice", text: msg, tone: "error" });
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (started.current) return; // StrictMode runs effects twice in dev; the handoff token is single-use.
    started.current = true;
    // The token rides in the URL fragment, which browsers never send to servers, proxies or access logs.
    const token = new URLSearchParams(window.location.hash.slice(1)).get("ctx");
    // Drop the token from the address bar and history right away.
    window.history.replaceState({}, "", "/chat");
    if (!token) {
      setFatal("Start from a search so the assistant knows what you need.");
      return;
    }
    api
      .createSession(token)
      .then((info) => {
        setSession(info);
        void send(info.session_id, { kickoff: true });
      })
      .catch((err) => setFatal(err instanceof ApiError ? err.message : "Couldn't start the chat."));
  }, [send]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [items]);

  const submit = (text: string) => {
    const message = text.trim();
    if (!message || !session || busy) return;
    dispatch({ type: "user", text: message });
    setDraft("");
    void send(session.session_id, { message });
  };

  const decide = (actionId: string, approved: boolean) => {
    if (!session || busy) return;
    void send(session.session_id, { confirmation: { action_id: actionId, approved } });
  };

  // Hide quick replies once an action is decided, or while an order waits for confirmation.
  const actionDone = items.some(
    (i) => i.kind === "confirm" && (i.state !== "pending" || i.tool === "submit_upgrade_order"),
  );
  const tools = items.filter((i): i is Extract<ChatItem, { kind: "tool" }> => i.kind === "tool");
  const assistant = config?.assistant_name ?? "Tidelink";
  const running = [...tools].reverse().find((t) => t.status === "running");
  const streaming = items.some((i) => i.kind === "bot" && i.streaming);
  const workingText = running ? `${FRIENDLY_TOOL[running.tool]?.working ?? "Working on it"}…` : "Typing…";

  if (fatal) {
    return (
      <div className="page chat-page">
        <header className="topbar">
          <BrandMark name={config?.app_name ?? "Tidelink"} tagline={config?.tagline} />
        </header>
        <main className="empty-state">
          <h1>{fatal}</h1>
          <Link to="/search" className="btn btn-primary">
            Go to search
          </Link>
        </main>
      </div>
    );
  }

  return (
    <div className="page chat-page">
      <header className="topbar">
        <BrandMark name={config?.app_name ?? "Tidelink"} tagline={config?.tagline} />
        <div className="chat-context">
          {session && (
            <>
              <span>
                {session.customer.first_name}, {session.customer.plan}
              </span>
              <span className="chat-query">From search: “{session.search_query}”</span>
            </>
          )}
        </div>
        <button type="button" className="btn btn-ghost trace-toggle" onClick={() => setShowTrace((v) => !v)} aria-expanded={showTrace}>
          {showTrace ? "Hide trace" : "MCP trace"}
        </button>
      </header>

      <div className={`chat-layout ${showTrace ? "trace-open" : ""}`}>
        <main className="chat-column">
          <ol className="messages" aria-live="polite">
            {!session && <li className="notice notice-info">Connecting you with {assistant}…</li>}
            {session && (
              <li className="intro">
                <Avatar size={56} state={busy && !items.length ? "thinking" : "idle"} label={`${assistant}, AI assistant`} />
                <div>
                  <p className="intro-name">
                    {assistant} <span className="ai-tag">AI assistant</span>
                  </p>
                  <p className="intro-text">
                    I can check your connection, explain your options in plain language, and take care of changes
                    once you say so. You can ask for a person at any time.
                  </p>
                </div>
              </li>
            )}
            {items.map((item, index) => {
              if (item.kind === "user")
                return (
                  <li key={item.id} className="msg msg-user">
                    <p>{item.text}</p>
                  </li>
                );
              if (item.kind === "bot") {
                const prev = items[index - 1];
                const showAvatar = !prev || prev.kind === "user";
                return (
                  <li key={item.id} className="bot-row">
                    <span className="bot-avatar-slot">
                      {showAvatar && <Avatar state={item.streaming ? "speaking" : "idle"} />}
                    </span>
                    <div className={`msg msg-bot ${item.streaming ? "streaming" : ""}`}>
                      {showAvatar && <span className="msg-author">{assistant}</span>}
                      <Markdown text={item.text} />
                    </div>
                  </li>
                );
              }
              if (item.kind === "tool")
                return (
                  <li key={item.id} className="msg-tool">
                    <ToolCard item={item} />
                  </li>
                );
              if (item.kind === "confirm")
                return (
                  <li key={item.id} className="msg-confirm">
                    <ConfirmCard item={item} disabled={busy} onDecide={(ok) => decide(item.id, ok)} />
                  </li>
                );
              return (
                <li key={item.id} className={`notice notice-${item.tone}`}>
                  {item.text}
                </li>
              );
            })}
            {busy && !streaming && (
              <li className="bot-row typing-row" aria-live="polite">
                <span className="bot-avatar-slot">
                  <Avatar state="thinking" />
                </span>
                <span className="typing-text">{workingText}</span>
              </li>
            )}
          </ol>

          <div className="composer-wrap">
            {session && !busy && !actionDone && session.scenario.suggested_replies.length > 0 && (
              <div className="suggestions">
                {session.scenario.suggested_replies.map((s) => (
                  <button key={s} type="button" className="chip" onClick={() => submit(s)}>
                    {s}
                  </button>
                ))}
              </div>
            )}
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                submit(draft);
              }}
            >
              <label htmlFor="msg" className="visually-hidden">
                Message {assistant}
              </label>
              <input
                id="msg"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={`Message ${assistant}`}
                maxLength={2000}
                autoComplete="off"
                disabled={!session}
              />
              <button type="submit" className="btn btn-primary" disabled={!session || busy || !draft.trim()}>
                Send
              </button>
            </form>
          </div>
          <div ref={endRef} />
        </main>

        <aside className="trace" aria-label="MCP tool trace">
          <h2>MCP trace</h2>
          <p className="trace-intro">Every tool call the assistant made, where the data came from, and how long it took.</p>
          {tools.length === 0 ? (
            <p className="trace-empty">No tool calls yet.</p>
          ) : (
            <ol className="trace-list">
              {tools.map((t) => (
                <li key={t.id} className={`trace-step trace-${t.status}`}>
                  <span className="trace-name">{t.tool}</span>
                  <SourceBadge source={t.source} fallback={t.fallback} />
                  <span className="trace-latency">{t.status === "running" ? "…" : `${t.latency} ms`}</span>
                </li>
              ))}
            </ol>
          )}
          {session && (
            <>
              <h3>Data sources</h3>
              <ul className="sources">
                {Object.entries(session.scenario.data_sources).map(([tool, src]) => (
                  <li key={tool}>
                    <span>{tool}</span>
                    <SourceBadge source={src} />
                  </li>
                ))}
              </ul>
              <p className="trace-foot">
                Scenario: {session.scenario.title}
                <br />
                <Link to="/dashboard">Open impact dashboard</Link>
              </p>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
