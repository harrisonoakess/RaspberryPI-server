/** Presentation helpers. The exact UTC string from the API is never discarded. */

/** Render an RFC 3339 UTC timestamp in the viewer's own timezone. */
export function formatLocalTime(utc: string): string {
  const parsed = new Date(utc);
  if (Number.isNaN(parsed.getTime())) {
    return utc;
  }
  return parsed.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

const UNITS = ["bytes", "KB", "MB", "GB"] as const;

/** Byte counts as a short human-readable size, exact for small values. */
export function formatBytes(size: number): string {
  if (!Number.isFinite(size) || size < 0) {
    return "—";
  }
  let value = size;
  let unit = 0;
  while (value >= 1024 && unit < UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rendered = unit === 0 ? String(value) : value.toFixed(1);
  return `${rendered} ${UNITS[unit]}`;
}
