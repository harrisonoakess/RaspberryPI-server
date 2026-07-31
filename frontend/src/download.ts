/** Handing a fetched payload to the browser's downloads.
 *
 * Kept out of `api.ts`, which is the network client and touches no DOM. The
 * name comes from here rather than from the response's `Content-Disposition`:
 * the `download` attribute wins for a blob URL regardless, so parsing the
 * header would be work whose result the browser ignores. The server still sends
 * the header for `curl` and any other direct client, and both names are
 * produced by the same rule.
 */

import type { DownloadedFile } from "./api";

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

/** `uploads-YYYYMMDD-HHMMSSZ.zip`, in UTC, matching the server's archive name. */
export function archiveFilename(now: Date): string {
  const stamp =
    `${pad(now.getUTCFullYear(), 4)}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}` +
    `-${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}Z`;
  return `uploads-${stamp}.zip`;
}

export function saveBlob({ blob, filename }: DownloadedFile): void {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    // Appended before clicking: a detached anchor is not reliably actioned.
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    URL.revokeObjectURL(url);
  }
}
