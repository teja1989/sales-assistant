import { Link } from "../router";

export function BrandMark({ name, tagline }: { name: string; tagline?: string }) {
  return (
    <Link to="/" className="brand">
      <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
        <circle cx="16" cy="16" r="14" fill="var(--teal)" />
        <path d="M7 13.5q2.25-2.6 4.5 0t4.5 0 4.5 0 4.5 0M7 19.5q2.25-2.6 4.5 0t4.5 0 4.5 0 4.5 0" stroke="#fff" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="brand-text">
        <span>{name}</span>
        {tagline ? <small className="brand-tagline">{tagline}</small> : null}
      </span>
    </Link>
  );
}
