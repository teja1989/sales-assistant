/**
 * Tidelink's illustrated avatar: friendly, but clearly an assistant, not a photo of a person.
 * States: idle (slow blink), thinking (eyes glance up, ripple pulse), speaking (gentle wave mouth).
 */
export type AvatarState = "idle" | "thinking" | "speaking";

export function Avatar({ state = "idle", size = 36, label }: { state?: AvatarState; size?: number; label?: string }) {
  return (
    <span
      className={`avatar avatar-${state}`}
      style={{ width: size, height: size }}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <svg viewBox="0 0 48 48" width={size} height={size}>
        <defs>
          <linearGradient id="avatar-sea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#3FA39A" />
            <stop offset="1" stopColor="#0F6E6A" />
          </linearGradient>
        </defs>
        <circle cx="24" cy="24" r="22" fill="url(#avatar-sea)" />
        {/* soft highlight */}
        <ellipse cx="17" cy="11" rx="8" ry="4" fill="#FFFFFF" opacity="0.14" />
        {/* eyes */}
        <g className="avatar-eyes" fill="#FFFDF8">
          <ellipse cx="17.5" cy="21" rx="2.6" ry="3.2" />
          <ellipse cx="30.5" cy="21" rx="2.6" ry="3.2" />
        </g>
        {/* mouth: a gentle smile */}
        <path
          className="avatar-mouth"
          d="M17.5 29.5q6.5 5.5 13 0"
          stroke="#FFFDF8"
          strokeWidth="2.4"
          fill="none"
          strokeLinecap="round"
        />
        {/* thinking ripple */}
        <circle className="avatar-ripple" cx="24" cy="24" r="22" fill="none" stroke="#3FA39A" strokeWidth="2" />
      </svg>
    </span>
  );
}

/** Plain-language names for tools, so the customer sees what is happening rather than API names. */
export const FRIENDLY_TOOL: Record<string, { title: string; working: string }> = {
  get_customer_profile: { title: "Your account", working: "Looking up your account" },
  check_area_outage: { title: "Outage check", working: "Checking for outages near you" },
  run_line_diagnostics: { title: "Connection check", working: "Checking your connection" },
  get_usage_profile: { title: "How you use your internet", working: "Looking at how you use your internet" },
  get_eligible_offers: { title: "Options for you", working: "Finding options that fit" },
  get_device_offer: { title: "Device details", working: "Pulling up the device details" },
  check_service_alerts: { title: "Alerts for your area", working: "Checking alerts for your area" },
  preview_order: { title: "Order summary", working: "Preparing your order summary" },
  submit_upgrade_order: { title: "Your order", working: "Placing your order" },
  reboot_gateway: { title: "Gateway restart", working: "Restarting your gateway" },
  schedule_technician: { title: "Technician visit", working: "Booking a technician" },
  apply_service_credit: { title: "Bill credit", working: "Adding your credit" },
  activate_storm_data_pass: { title: "Storm data pass", working: "Turning on your storm data pass" },
  get_account_checkup: { title: "Account checkup", working: "Looking over your account" },
  enroll_autopay: { title: "Autopay", working: "Turning on autopay" },
  return_unused_equipment: { title: "Equipment return", working: "Starting your return" },
  update_gateway_firmware: { title: "Gateway update", working: "Scheduling your gateway update" },
};

/** Confirmation headings phrased as a question to the customer. */
export const CONFIRM_HEADING: Record<string, string> = {
  submit_upgrade_order: "Ready to place your order?",
  reboot_gateway: "Restart your gateway now?",
  schedule_technician: "Book this technician visit?",
  apply_service_credit: "Add this credit to your bill?",
  activate_storm_data_pass: "Turn on your storm data pass?",
  enroll_autopay: "Turn on autopay and paperless?",
  return_unused_equipment: "Return this equipment?",
  update_gateway_firmware: "Schedule the gateway update?",
};
