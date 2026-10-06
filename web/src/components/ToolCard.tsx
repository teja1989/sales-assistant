import type { ChatItem, Json } from "../types";

type ToolItem = Extract<ChatItem, { kind: "tool" }>;

const money = (v: unknown) =>
  typeof v === "number"
    ? `${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : "";
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
              {o.bundle ? <div className="offer-bundle">+ {str(obj(o.bundle).name)}</div> : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function DeviceOffer({ data }: { data: Json }) {
  const device = obj(data.device);
  const pricing = obj(data.pricing);
  const trade = obj(data.trade_in);
  const offer = obj(data.offer);
  const highlights = (Array.isArray(device.highlights) ? device.highlights : []) as string[];
  return (
    <div className="tool-body">
      <div className="device-head">
        <span className="offer-name">{str(device.name)}</span>
        <span className="offer-price">
          {money(pricing.full_price)}
          <span> from</span>
        </span>
      </div>
      <ul className="device-highlights">
        {highlights.map((h) => (
          <li key={h}>{h}</li>
        ))}
      </ul>
      {trade.eligible ? (
        <dl className="receipt">
          <div>
            <dt>Trade-in: {str(trade.current_device)}</dt>
            <dd>{money(trade.usual_credit)}</dd>
          </div>
          <div className="receipt-bonus">
            <dt>Valued-customer bonus</dt>
            <dd>+{money(trade.valued_customer_bonus_credit)}</dd>
          </div>
          <div className="receipt-total">
            <dt>Your price after trade-in</dt>
            <dd>
              {money(offer.price_after_trade_in)} or {money(offer.monthly_installment_after_trade_in)}/mo
            </dd>
          </div>
        </dl>
      ) : null}
      <p className="source-note">Device facts: {str(device.facts_source)}. Trade-in and financing simulated.</p>
    </div>
  );
}

function Alerts({ data }: { data: Json }) {
  const alerts = (Array.isArray(data.alerts) ? data.alerts : []) as Json[];
  if (!alerts.length) return <p className="tool-summary">No weather or network alerts for {str(data.service_area)}.</p>;
  const a = alerts[0];
  const courtesy = obj(data.courtesy);
  return (
    <div className="tool-body">
      <p className="verdict verdict-warn">{str(a.headline)}</p>
      <p className="tool-summary">
        {str(data.service_area)}, {str(a.window)}. {str(a.expected_impact)}.
      </p>
      {courtesy.storm_data_pass_eligible ? (
        <p className="tool-summary">Eligible: free unlimited mobile data for {str(courtesy.hours)} hours.</p>
      ) : null}
    </div>
  );
}

function Preview({ data }: { data: Json }) {
  const items = (Array.isArray(data.line_items) ? data.line_items : []) as Json[];
  const benefits = (Array.isArray(data.included_benefits) ? data.included_benefits : []) as string[];
  return (
    <div className="tool-body">
      <dl className="receipt">
        {items.map((it, i) => (
          <div key={i}>
            <dt>{str(it.label)}</dt>
            <dd>{it.worth_monthly_price != null ? `Free (worth ${money(it.worth_monthly_price)}/mo)` : money(it.amount)}</dd>
          </div>
        ))}
        {data.monthly_installment != null && (
          <div>
            <dt>Monthly installment</dt>
            <dd>
              {money(data.monthly_installment)} × {str(data.installment_months)}
            </dd>
          </div>
        )}
        <div className="receipt-total">
          <dt>New monthly bill</dt>
          <dd>{money(data.new_monthly_price)}</dd>
        </div>
        <div>
          <dt>Due today</dt>
          <dd>{money(data.due_today)}</dd>
        </div>
      </dl>
      {benefits.length > 0 && <p className="offer-bundle">Included: {benefits.join("; ")}</p>}
      <p className="source-note">
        Quote {str(data.quote_id)}. {str(data.note)}
      </p>
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
  if (tool === "submit_upgrade_order") {
    const benefits = (Array.isArray(data.included_benefits) ? data.included_benefits : []) as string[];
    return (
      <>
        <p className="tool-summary">
          Order {str(data.order_id)}: {str(data.item)}. {str(data.effective)}.
        </p>
        {benefits.length > 0 && <p className="offer-bundle">Active: {benefits.join("; ")}</p>}
      </>
    );
  }
  if (tool === "activate_storm_data_pass")
    return (
      <p className="tool-summary">
        Free unlimited data on {str(data.lines_covered)} lines for {str(data.hours)} hours. Ends automatically.
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
        ) : item.tool === "get_device_offer" ? (
          <DeviceOffer data={data} />
        ) : item.tool === "check_service_alerts" ? (
          <Alerts data={data} />
        ) : item.tool === "preview_order" ? (
          <Preview data={data} />
        ) : (
          <Result tool={item.tool} data={data} />
        ))}
    </div>
  );
}
