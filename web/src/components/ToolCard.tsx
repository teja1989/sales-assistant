import type { ChatItem, Json } from "../types";

type ToolItem = Extract<ChatItem, { kind: "tool" }>;

const money = (v: unknown) => (typeof v === "number" ? `$${v.toFixed(2)}` : "");
const num = (v: unknown) => (typeof v === "number" ? v : undefined);
const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));
const obj = (v: unknown) => (v && typeof v === "object" ? (v as Json) : {});

const VERDICT_LABEL: Record<string, string> = {
  area_outage: "Area outage",
  gateway_fault: "Gateway fault",
  signal_issue: "Signal problem",
  wifi_coverage: "Weak Wi-Fi coverage",
  plan_capacity: "Outgrowing plan",
  healthy: "Healthy",
};

function Diagnostics({ data }: { data: Json }) {
  const verdict = str(data.verdict);
  const signal = obj(data.signal);
  const wifi = obj(data.wifi);
  const tone = verdict === "healthy" || verdict === "plan_capacity" ? "ok" : verdict === "wifi_coverage" ? "warn" : "bad";
  return (
    <div className="tool-body">
      <p className={`verdict verdict-${tone}`}>{VERDICT_LABEL[verdict] ?? verdict}</p>
      <p className="tool-summary">{str(data.summary)}</p>
      <dl className="facts">
        <div>
          <dt>Drops (24h)</dt>
          <dd>{str(data.connection_drops_24h)}</dd>
        </div>
        <div>
          <dt>Signal</dt>
          <dd>{signal.within_spec ? "In spec" : "Out of spec"}</dd>
        </div>
        <div>
          <dt>Speed</dt>
          <dd>{data.measured_speed_mbps != null ? `${str(data.measured_speed_mbps)} Mbps` : "n/a"}</dd>
        </div>
        <div>
          <dt>Wi-Fi</dt>
          <dd>
            {str(wifi.coverage)}
            {wifi.weakest_room ? `, weakest: ${str(wifi.weakest_room)}` : ""}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function Outage({ data }: { data: Json }) {
  if (!data.outage_active) return <p className="tool-summary">No outage in {str(data.service_area) || "the area"}.</p>;
  return (
    <div className="tool-body">
      <p className="verdict verdict-bad">Outage in {str(data.service_area)}</p>
      <p className="tool-summary">
        Cause: {str(data.cause)}. Estimated restore: about {str(data.estimated_restore_minutes)} min.
        {data.homes_affected ? ` ${str(data.homes_affected)} homes affected.` : ""}
      </p>
    </div>
  );
}

function Usage({ data }: { data: Json }) {
  const pct = num(data.peak_utilization_pct) ?? 0;
  return (
    <div className="tool-body">
      <div className="meter" role="img" aria-label={`Peak usage ${pct}% of plan`}>
        <div className={`meter-fill ${pct >= 85 ? "meter-hot" : ""}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <p className="tool-summary">
        Peak use <strong>{pct}%</strong> of {str(data.plan_download_mbps)} Mbps
        {data.hours_at_plan_limit_30d ? `, at the limit ${str(data.hours_at_plan_limit_30d)} h in 30 days` : ""}.
      </p>
    </div>
  );
}

function Offers({ data }: { data: Json }) {
  if (data.blocked) {
    return (
      <div className="tool-body">
        <p className="verdict verdict-warn">Offers paused</p>
        <p className="tool-summary">Fix first, sell later: an open service issue blocks upsell.</p>
      </div>
    );
  }
  const offers = (Array.isArray(data.offers) ? data.offers : []) as Json[];
  return (
    <div className="tool-body">
      {data.advisory ? <p className="tool-summary advisory">{str(data.advisory)}</p> : null}
      <ul className="offers">
        {offers.map((o) => {
          const promo = obj(o.promo);
          return (
            <li key={str(o.offer_id)} className={o.recommended ? "offer offer-recommended" : "offer"}>
              <div className="offer-head">
                <span className="offer-name">{str(o.name)}</span>
                {o.recommended ? <span className="offer-badge">Recommended</span> : null}
              </div>
              <div className="offer-price">
                {money(o.monthly_price)}
                <span>/mo</span>
              </div>
              {promo.promo_monthly_price != null && (
                <div className="offer-promo">
                  {money(promo.promo_monthly_price)}/mo for {str(promo.months)} months
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function Result({ tool, data }: { tool: string; data: Json }) {
  if (tool === "reboot_gateway")
    return (
      <p className="tool-summary">
        {data.issue_resolved ? "Gateway restarted and the fault cleared." : "Gateway restarted; the fault remains."}
      </p>
    );
  if (tool === "apply_service_credit")
    return <p className="tool-summary">Credit {money(data.amount)} added to the {str(data.applies_to)}.</p>;
  if (tool === "schedule_technician")
    return (
      <p className="tool-summary">
        Visit booked: {str(data.appointment_window)} (ticket {str(data.ticket_id)}).
      </p>
    );
  if (tool === "submit_upgrade_order")
    return (
      <p className="tool-summary">
        Order {str(data.order_id)}: {str(data.item)}. {str(data.effective)}.
      </p>
    );
  if (tool === "get_customer_profile") {
    const plan = obj(data.plan);
    return (
      <p className="tool-summary">
        {str(data.first_name)}, {str(plan.name)} ({str(plan.download_mbps)} Mbps, {money(plan.monthly_price)}/mo),
        customer for {str(data.tenure_months)} months.
      </p>
    );
  }
  return (
    <dl className="facts">
      {Object.entries(data)
        .slice(0, 6)
        .map(([k, v]) => (
          <div key={k}>
            <dt>{k.replace(/_/g, " ")}</dt>
            <dd>{typeof v === "object" ? JSON.stringify(v) : str(v)}</dd>
          </div>
        ))}
    </dl>
  );
}

export function SourceBadge({ source, fallback }: { source: string; fallback?: boolean }) {
  return (
    <span className={`source source-${source}`} title={fallback ? "Live source unavailable; used simulator" : undefined}>
      {fallback ? "sim (fallback)" : source}
    </span>
  );
}

export function ToolCard({ item }: { item: ToolItem }) {
  const data = item.data ?? {};
  return (
    <div className={`tool-card tool-${item.status}`}>
      <div className="tool-head">
        <span className="tool-dot" aria-hidden="true" />
        <span className="tool-title">{item.title}</span>
        <SourceBadge source={item.source} fallback={item.fallback} />
        {item.status === "running" ? (
          <span className="tool-meta">checking</span>
        ) : (
          <span className="tool-meta">{item.latency} ms</span>
        )}
      </div>
      {item.status === "error" && <p className="tool-summary error-text">{str(data.error) || "This check failed."}</p>}
      {item.status === "done" &&
        (item.tool === "run_line_diagnostics" ? (
          <Diagnostics data={data} />
        ) : item.tool === "check_area_outage" ? (
          <Outage data={data} />
        ) : item.tool === "get_usage_profile" ? (
          <Usage data={data} />
        ) : item.tool === "get_eligible_offers" ? (
          <Offers data={data} />
        ) : (
          <Result tool={item.tool} data={data} />
        ))}
    </div>
  );
}
