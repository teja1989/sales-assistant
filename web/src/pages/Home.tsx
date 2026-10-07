import { useEffect, useState } from "react";
import { api } from "../api";
import { Link, navigate } from "../router";
import { BrandMark } from "../components/BrandMark";
import type { AppConfig, ScenarioView } from "../types";

export function Home({ config }: { config: AppConfig | null }) {
  const [scenarios, setScenarios] = useState<ScenarioView[]>([]);
  useEffect(() => {
    api.scenarios().then(setScenarios).catch(() => setScenarios([]));
  }, []);

  return (
    <div className="page home">
      <header className="topbar">
        <BrandMark name={config?.app_name ?? "Tidelink"} tagline={config?.tagline} />
        <nav>
          <Link to="/search">Search handoff</Link>
          <Link to="/dashboard">Impact</Link>
        </nav>
      </header>

      <main className="home-main">
        <section className="home-intro">
          <h1>{config?.tagline ?? "Always on, like the tide."}</h1>
          <p className="lede lede-strong">
            Reliable home internet and mobile that keeps you connected, every hour of every day.
          </p>
          <p className="lede">
            Customers arrive from a search assistant with their question already known. {config?.assistant_name ?? "Tidelink"} checks
            the account, outage map and line health through MCP tools, fixes what it can, and recommends an upgrade only
            when the data says the customer is outgrowing their plan.
          </p>
          <div className="home-actions">
            <Link to="/search" className="btn btn-primary">
              Start from a search
            </Link>
            <Link to="/dashboard" className="btn btn-ghost">
              See the impact
            </Link>
          </div>
          {config && (
            <p className="runtime">
              Model: <strong>{config.llm === "mock" ? "offline mock" : "Azure OpenAI"}</strong>. Data:{" "}
              <strong>{config.data_source === "live" ? `live MCP (${config.live_host})` : "simulator"}</strong>.
            </p>
          )}
        </section>

        <DemoLauncher scenarios={scenarios} live={config?.data_source === "live"} />
      </main>
    </div>
  );
}

const INTENT_LABEL: Record<string, string> = {
  account: "Account",
  connectivity: "Fix",
  speed: "Upgrade",
  wifi: "Fix + right product",
  device: "Device sale",
  alert: "Proactive care",
  general: "General",
};

/**
 * Demo launcher: one card per simulated customer, in the order of the demo script. "Start from search"
 * tells the full story (external search → handoff → sign-in); "Skip to sign-in" jumps straight to the
 * chat's sign-in step with the matching account preselected.
 */
function DemoLauncher({ scenarios, live }: { scenarios: ScenarioView[]; live: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function skipToSignIn(s: ScenarioView) {
    setBusy(s.id);
    setError(null);
    try {
      const res = await api.handoff(s.search_query, s.id);
      navigate(`/chat#ctx=${encodeURIComponent(res.token)}&s=${encodeURIComponent(res.scenario.id)}`);
    } catch {
      setError("Couldn't start that demo. Check that the app is running and try again.");
      setBusy(null);
    }
  }

  return (
    <section className="launcher" aria-labelledby="launcher-heading">
      <div className="launcher-head">
        <h2 id="launcher-heading">{live ? "Start a conversation" : "Demo launcher"}</h2>
        <p>
          {live
            ? "Live data: each card starts from a typical search, and you sign in with a real (or test) account number. Answers come from your MCP server."
            : "Each card is a simulated customer signed in with their own account. Run them top to bottom to follow the demo script."}
        </p>
      </div>
      {error && (
        <p className="search-error" role="alert">
          {error}
        </p>
      )}
      <ol className="launcher-grid">
        {scenarios.map((s, i) => {
          const name = s.persona?.first_name ?? "Customer";
          return (
            <li key={s.id} className="persona-card">
              <div className="persona-top">
                <span className={`persona-badge tone-${i % 5}`} aria-hidden="true">
                  {live ? "•" : name.charAt(0)}
                </span>
                <div className="persona-who">
                  <strong>{live ? "Your account" : name}</strong>
                  <span>{live ? "Live data" : s.persona?.plan}</span>
                </div>
                <span className="persona-step" aria-label={`Step ${i + 1}`}>
                  {i + 1}
                </span>
              </div>
              <span className="persona-tag">{INTENT_LABEL[s.intent] ?? s.intent}</span>
              <h3>{s.title}</h3>
              {!live && <p className="persona-desc">{s.description}</p>}
              <p className="persona-query">“{s.search_query}”</p>
              <div className="persona-actions">
                <Link to={`/search?scenario=${encodeURIComponent(s.id)}`} className="btn btn-primary btn-sm">
                  Start from search
                </Link>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  disabled={busy !== null}
                  onClick={() => void skipToSignIn(s)}
                >
                  {busy === s.id ? "Opening" : "Skip to sign-in"}
                </button>
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
