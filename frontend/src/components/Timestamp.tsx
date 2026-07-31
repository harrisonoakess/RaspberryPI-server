import { formatLocalTime } from "../format";

/** Local time on screen; the exact UTC value stays in the accessible tooltip. */
export function Timestamp({ utc }: { utc: string }) {
  return (
    <time dateTime={utc} title={utc} aria-label={`${formatLocalTime(utc)} (${utc})`}>
      {formatLocalTime(utc)}
    </time>
  );
}
