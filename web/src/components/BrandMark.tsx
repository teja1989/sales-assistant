import { Link } from "../router";

export function BrandMark({ name }: { name: string }) {
  return (
    <Link to="/" className="brand">
      <svg viewBox="0 0 32 32" width="28" height="28" aria-hidden="true">
        <circle cx="16" cy="16" r="14" fill="var(--teal)" />
        <path d="M9 18a10 10 0 0 1 14 0M12 21.5a5 5 0 0 1 8 0" stroke="#fff" strokeWidth="2.4" fill="none" strokeLinecap="round" />
      </svg>
      <span>{name}</span>
    </Link>
  );
}
