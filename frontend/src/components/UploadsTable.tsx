/** The upload rows themselves, with sortable column headers.
 *
 * Used twice: as the flat file list, and inside an expanded card group where
 * the card and device columns would repeat one value on every row.
 */

import { SORT_KEYS, type SortKey, type SortOrder, type UploadItem } from "../api";
import { formatBytes } from "../format";
import { PreviewDialog } from "./PreviewDialog";
import { Timestamp } from "./Timestamp";
import styles from "./UploadsTable.module.css";

interface Column {
  key: SortKey;
  label: string;
  numeric?: boolean;
}

// `filename` leads because it is the row header, which must come first for a
// screen reader to announce the rest of the row against it.
const COLUMNS: readonly Column[] = [
  { key: "filename", label: "File" },
  { key: "card_uuid", label: "Card UUID" },
  { key: "device_id", label: "Device" },
  { key: "size", label: "Size", numeric: true },
  { key: "received_at", label: "Received" },
];

/** Sizes and times read best largest/newest first; names read best A to Z. */
export function defaultOrderFor(key: SortKey): SortOrder {
  return key === "size" || key === "received_at" ? "desc" : "asc";
}

const ARIA_SORT = { asc: "ascending", desc: "descending" } as const;

interface UploadsTableProps {
  items: UploadItem[];
  sort: SortKey;
  order: SortOrder;
  onSort: (key: SortKey) => void;
  onUnauthorized: () => void;
  caption: string;
  /** Columns whose value is already fixed by the surrounding view. */
  hidden?: readonly SortKey[];
}

export function UploadsTable({
  items,
  sort,
  order,
  onSort,
  onUnauthorized,
  caption,
  hidden = [],
}: UploadsTableProps) {
  const columns = COLUMNS.filter((column) => !hidden.includes(column.key));

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => {
              const active = column.key === sort;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={active ? ARIA_SORT[order] : "none"}
                  className={column.numeric ? styles.numericHeader : undefined}
                >
                  <button
                    type="button"
                    className={active ? `${styles.sortButton} ${styles.sorted}` : styles.sortButton}
                    onClick={() => onSort(column.key)}
                  >
                    {column.label}
                    <span aria-hidden="true" className={styles.arrow}>
                      {active ? (order === "asc" ? "↑" : "↓") : "↕"}
                    </span>
                  </button>
                </th>
              );
            })}
            <th scope="col">
              <span className={styles.visuallyHidden}>Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <th scope="row" className={styles.filename}>
                {item.filename}
              </th>
              {!hidden.includes("card_uuid") && (
                <td className={styles.mono}>{item.card_uuid}</td>
              )}
              {!hidden.includes("device_id") && <td>{item.device_id}</td>}
              <td className={styles.numeric}>{formatBytes(item.size)}</td>
              <td>
                <Timestamp utc={item.received_at} />
              </td>
              <td>
                <PreviewDialog upload={item} onUnauthorized={onUnauthorized} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Narrow an arbitrary string to a sort key, for values read back from the API. */
export function isSortKey(value: string): value is SortKey {
  return (SORT_KEYS as readonly string[]).includes(value);
}
