import type { ChatItem, Json } from "../types";

type ConfirmItem = Extract<ChatItem, { kind: "confirm" }>;

const money = (v: unknown) => (typeof v === "number" ? `$${v.toFixed(2)}` : null);

function Details({ details }: { details: Json }) {
  const promo = (details.promo ?? null) as Json | null;
  const rows: [string, string][] = [];
  const price = money(details.monthly_price);
  if (price) rows.push(["Monthly price", price]);
  const change = money(details.monthly_change);
  if (change && details.type !== "equipment") rows.push(["Change vs today", `+${change}/mo`]);
  if (promo && money(promo.promo_monthly_price))
    rows.push([`First ${String(promo.months)} months`, `${money(promo.promo_monthly_price)}/mo`]);
  const amount = money(details.amount);
  if (amount) rows.push(["Credit", amount]);
  if (typeof details.issue === "string" && details.issue) rows.push(["Issue", details.issue]);
  if (!rows.length) return null;
  return (
    <dl className="confirm-details">
      {rows.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

const STATE_TEXT: Record<ConfirmItem["state"], string> = {
  pending: "",
  approved: "Confirmed",
  declined: "Skipped",
  superseded: "Replaced by a newer request",
};

export function ConfirmCard({
  item,
  disabled,
  onDecide,
}: {
  item: ConfirmItem;
  disabled: boolean;
  onDecide: (approved: boolean) => void;
}) {
  return (
    <div className={`confirm-card confirm-${item.state}`} role="group" aria-label={item.title}>
      <p className="confirm-title">{item.title}</p>
      <p className="confirm-summary">{item.summary}</p>
      <Details details={item.details} />
      {item.state === "pending" ? (
        <div className="confirm-actions">
          <button type="button" className="btn btn-primary" disabled={disabled} onClick={() => onDecide(true)}>
            Confirm
          </button>
          <button type="button" className="btn btn-ghost" disabled={disabled} onClick={() => onDecide(false)}>
            Not now
          </button>
        </div>
      ) : (
        <p className="confirm-state">{STATE_TEXT[item.state]}</p>
      )}
      {item.state === "pending" && <p className="confirm-note">Nothing changes on your account until you confirm.</p>}
    </div>
  );
}
