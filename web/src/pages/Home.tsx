import { useEffect, useState } from "react";
import { api } from "../api";
import { Link } from "../router";
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
        <BrandMark name={config?.app_name ?? "Tidelink Assist"} tagline={config?.tagline} />
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
            Customers arrive from a search assistant with their question already known. {config?.assistant_name ?? "Tide"} checks
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
              Model: <strong>{config.llm}</strong>. Live MCP: <strong>{config.live_configured ? config.live_host : "not connected (simulator only)"}</strong>
              {config.data_source_override && (
                <>
                  . Forced data source: <strong>{config.data_source_override}</strong>
                </>
              )}
            </p>
          )}
        </section>

        <section className="scenario-list" aria-labelledby="scenarios-heading">
          <h2 id="scenarios-heading">Demo scenarios</h2>
          <ul>
            {scenarios.map((s) => (
              <li key={s.id}>
                <Link to={`/search?scenario=${encodeURIComponent(s.id)}`} className="scenario-row">
                  <span className="scenario-title">{s.title}</span>
                  <span className="scenario-desc">{s.description}</span>
                  <span className="scenario-query">“{s.search_query}”</span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
