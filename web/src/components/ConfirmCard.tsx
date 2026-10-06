import type { ChatItem, Json } from "../types";

type ConfirmItem = Extract<ChatItem, { kind: "confirm" }>;

const money = (v: unknown) =>
  typeof v === "number"
    ? `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : null;

function Details({ details }: { details: Json }) {
  const promo = (details.promo ?? null) as Json | null;
  const rows: [string, string][] = [];
  if (details.type === "device") {
    if (money(details.full_price)) rows.push(["Device price", money(details.full_price)!]);
    if (money(details.trade_in_credit)) rows.push(["Trade-in credit", `-${money(details.trade_in_credit)}`]);
    if (money(details.monthly_installment))
      rows.push(["Monthly installment", `${money(details.monthly_installment)} × ${String(details.installment_months)}`]);
  } else {
    if (money(details.current_monthly_price)) rows.push(["Monthly today", money(details.current_monthly_price)!]);
    const hasPromo = promo && money(promo.promo_monthly_price) && details.type === "plan_upgrade";
    if (money(details.new_monthly_price))
      rows.push([hasPromo ? `New monthly, first ${String(promo!.months)} months` : "New monthly", money(details.new_monthly_price)!]);
    else if (money(details.monthly_price)) rows.push(["Monthly price", money(details.monthly_price)!]);
    if (hasPromo && money(details.monthly_price)) rows.push(["After the promo", money(details.monthly_price)!]);
  }
  if (money(details.due_today)) rows.push(["Due today", money(details.due_today)!]);
  const amount = money(details.amount);
  if (amount) rows.push(details.amount === 0 ? ["Cost to you", "Free"] : ["Credit", amount]);
  if (typeof details.issue === "string" && details.issue) rows.push(["Issue", details.issue]);
  const benefits = (Array.isArray(details.included_benefits) ? details.included_benefits : []) as string[];
  if (!rows.length && !benefits.length) return null;
  return (
    <>
      <dl className="confirm-details">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {benefits.length > 0 && <p className="offer-bundle">Included: {benefits.join("; ")}</p>}
    </>
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
