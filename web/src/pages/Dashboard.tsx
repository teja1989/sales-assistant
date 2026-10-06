import { useEffect, useState } from "react";
import { api } from "../api";
import { Link } from "../router";
import { BrandMark } from "../components/BrandMark";
import type { AppConfig, Metrics } from "../types";

const pct = (v: number) => `${Math.round(v * 100)}%`;
const usd = (v: number) => `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const EVENT_TEXT: Record<string, string> = {
  session_started: "Chat started",
  fault_detected: "Fault detected",
  upsell_blocked: "Upsell blocked until fixed",
  offer_presented: "Offer presented",
  offer_accepted: "Offer accepted",
  truck_roll_avoided: "Truck roll avoided",
  credit_applied: "Credit applied",
  technician_booked: "Technician booked",
  action_confirmed: "Customer confirmed",
  action_declined: "Customer declined",
  guardrail: "Guardrail stepped in",
  tool_error: "Tool error",
  order_previewed: "Order previewed",
  storm_pass_activated: "Storm data pass on",
  checkup: "Account checkup",
  account_fix: "Account item fixed",
};

export function Dashboard({ config }: { config: AppConfig | null }) {
  const [m, setM] = useState<Metrics | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .metrics()
        .then((data) => alive && (setM(data), setError(false)))
        .catch(() => alive && setError(true));
    void load();
    const timer = window.setInterval(load, 3000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <div className="page dash-page">
      <header className="topbar">
        <BrandMark name={config?.app_name ?? "Tidelink"} tagline={config?.tagline} />
        <nav>
          <Link to="/search">Search handoff</Link>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => api.resetMetrics().then(() => api.metrics().then(setM))}
          >
            Reset numbers
          </button>
        </nav>
      </header>

      <main className="dash-main">
        <h1>Impact so far</h1>
        <p className="lede">
          Counted from what actually happened in each chat: tool results and confirmed actions, never from what the model
          said. {m?.note}
        </p>
        {error && <p className="notice notice-error">Can't reach the metrics endpoint. Is the app running?</p>}

        {m && (
          <>
            <section className="headline" aria-label="Support and sales outcomes">
              <div className="headline-main">
                <span className="figure">{pct(m.containment_rate)}</span>
                <span className="figure-label">
                  of {m.sessions} {m.sessions === 1 ? "chat" : "chats"} resolved without a human agent ({m.resolved_without_agent})
                </span>
              </div>
              <dl className="headline-side">
                <div>
                  <dt>Truck rolls avoided</dt>
                  <dd>
                    {m.truck_rolls_avoided}
                    <small>
                      about {usd(m.estimated_field_cost_avoided_usd)} saved at {usd(m.truck_roll_cost_assumption_usd)} each
                    </small>
                  </dd>
                </div>
                <div>
                  <dt>Offer conversion</dt>
                  <dd>
                    {pct(m.offer_conversion_rate)}
                    <small>
                      {m.offers_accepted} accepted of {m.offers_presented} presented
                    </small>
                  </dd>
                </div>
                <div>
                  <dt>New monthly revenue</dt>
                  <dd>
                    {usd(m.incremental_monthly_revenue_usd)}
                    <small>recurring, from accepted offers</small>
                  </dd>
                </div>
                <div>
                  <dt>Upsell held back</dt>
                  <dd>
                    {m.upsell_blocked_fault_first}
                    <small>times an offer waited for a fix first</small>
                  </dd>
                </div>
              </dl>
            </section>

            <section className="ops" aria-label="Account checkups">
              <h2>Account checkups</h2>
              <dl>
                <div><dt>Checkups after sign-in</dt><dd>{m.checkups_run}</dd></div>
                <div><dt>Issues found</dt><dd>{m.account_issues_found}</dd></div>
                <div><dt>Issues fixed in chat</dt><dd>{m.account_issues_fixed}</dd></div>
                <div><dt>Savings found / realized</dt><dd>{usd(m.monthly_savings_found_usd)} / {usd(m.monthly_savings_realized_usd)}</dd></div>
              </dl>
            </section>

            <section className="ops" aria-label="Mobile and cross-sell">
              <h2>Mobile and cross-sell</h2>
              <dl>
                <div><dt>Devices sold</dt><dd>{m.devices_sold}</dd></div>
                <div><dt>Device sales</dt><dd>{usd(m.device_sales_usd)}</dd></div>
                <div><dt>Trade-in credits given</dt><dd>{usd(m.trade_in_credits_usd)}</dd></div>
                <div><dt>Free-year mobile bundles</dt><dd>{m.mobile_bundles}</dd></div>
                <div><dt>New mobile lines</dt><dd>{m.new_mobile_lines}</dd></div>
                <div><dt>Storm data passes</dt><dd>{m.storm_data_passes}</dd></div>
                <div><dt>Orders previewed / placed</dt><dd>{m.orders_previewed} / {m.offers_accepted}</dd></div>
                <div><dt>Offers presented</dt><dd>{m.offers_presented}</dd></div>
              </dl>
            </section>

            <section className="ops" aria-label="Trust and operations">
              <h2>Trust and operations</h2>
              <dl>
                <div><dt>Faults found remotely</dt><dd>{m.faults_detected}</dd></div>
                <div><dt>Credits issued</dt><dd>{usd(m.credits_issued_usd)}</dd></div>
                <div><dt>Technician visits booked</dt><dd>{m.technician_visits_booked}</dd></div>
                <div><dt>Actions confirmed / declined</dt><dd>{m.actions_confirmed} / {m.actions_declined}</dd></div>
                <div><dt>Guardrail interventions</dt><dd>{m.guardrail_interventions}</dd></div>
                <div><dt>MCP tool calls (sim / live)</dt><dd>{m.tool_calls_sim} / {m.tool_calls_live}</dd></div>
                <div><dt>Tool errors</dt><dd>{m.tool_errors}</dd></div>
                <div>
                  <dt>Avg time to resolution</dt>
                  <dd>{m.avg_time_to_resolution_s != null ? `${m.avg_time_to_resolution_s}s` : "n/a"}</dd>
                </div>
              </dl>
            </section>

            <div className="dash-split">
              <section aria-label="By scenario">
                <h2>By scenario</h2>
                {Object.keys(m.by_scenario).length === 0 ? (
                  <p className="trace-empty">
                    No chats yet. <Link to="/search">Start one from search.</Link>
                  </p>
                ) : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">Scenario</th>
                        <th scope="col">Chats</th>
                        <th scope="col">Resolved</th>
                        <th scope="col">Offers accepted</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(m.by_scenario).map(([id, c]) => (
                        <tr key={id}>
                          <th scope="row">{id}</th>
                          <td>{c.sessions ?? 0}</td>
                          <td>{c.resolved_without_agent ?? 0}</td>
                          <td>{c.offers_accepted ?? 0}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </section>
              <section aria-label="Recent events">
                <h2>Live events</h2>
                <ol className="events">
                  {m.recent_events.map((e, i) => (
                    <li key={`${e.ts}-${i}`} className={`event event-${e.kind}`}>
                      <time>{new Date(e.ts * 1000).toLocaleTimeString()}</time>
                      <span className="event-kind">{EVENT_TEXT[e.kind] ?? e.kind}</span>
                      <span className="event-detail">{e.detail}</span>
                    </li>
                  ))}
                </ol>
              </section>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
