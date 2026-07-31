/** The upload rows themselves, with sortable column headers.
 *
 * Used twice: as the flat file list, and inside an expanded card group where
 * the card and device columns would repeat one value on every row.
 */

import { SORT_KEYS, type SortKey, type SortOrder, type UploadItem } from "../api";
import { formatBytes } from "../format";
import { DownloadButton } from "./DownloadButton";
import { PreviewDialog } from "./PreviewDialog";
import { Timestamp } from "./Timestamp";
import styles from "./UploadsTable.module.css";

interface Column {
  key: SortKey;
  label: string;
  numeric?: boolean;
}

// `filename` leads the data columns because it is the row header, which must
// come first for a screen reader to announce the rest of the row against it.
// The selection checkbox, when present, sits ahead of it in a plain cell: it
// carries its own name, so it does not depend on the row header, and the left
// edge is where a pointer user looks for it.
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

/** Bulk-selection wiring. Omitted entirely by views where "this page" has no
 * meaning, in which case no checkbox column is rendered at all. */
export interface TableSelection {
  selected: ReadonlySet<number>;
  onToggle: (id: number, selected: boolean) => void;
  onToggleAll: (selected: boolean) => void;
}

interface UploadsTableProps {
  items: UploadItem[];
  sort: SortKey;
  order: SortOrder;
  onSort: (key: SortKey) => void;
  onUnauthorized: () => void;
  caption: string;
  /** Columns whose value is already fixed by the surrounding view. */
  hidden?: readonly SortKey[];
  selection?: TableSelection;
}

export function UploadsTable({
  items,
  sort,
  order,
  onSort,
  onUnauthorized,
  caption,
  hidden = [],
  selection,
}: UploadsTableProps) {
  const columns = COLUMNS.filter((column) => !hidden.includes(column.key));

  const selectedCount =
    selection === undefined
      ? 0
      : items.reduce((total, item) => (selection.selected.has(item.id) ? total + 1 : total), 0);
  const allSelected = items.length > 0 && selectedCount === items.length;
  const someSelected = selectedCount > 0 && !allSelected;

  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>{caption}</caption>
        <thead>
          <tr>
            {selection !== undefined && (
              <th scope="col" className={styles.selectCell}>
                <input
                  type="checkbox"
                  checked={allSelected}
                  // React cannot express `indeterminate` as an attribute. A
                  // native checkbox in that state already maps to
                  // `aria-checked="mixed"`, so no ARIA override is added.
                  ref={(node) => {
                    if (node !== null) {
                      node.indeterminate = someSelected;
                    }
                  }}
                  aria-label="Select all files on this page"
                  onChange={() => selection.onToggleAll(!allSelected)}
                />
              </th>
            )}
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
              {selection !== undefined && (
                // A plain cell, not a second row header: the row's header is
                // its filename.
                <td className={styles.selectCell}>
                  <input
                    type="checkbox"
                    checked={selection.selected.has(item.id)}
                    aria-label={`Select ${item.filename} from card ${item.card_uuid}`}
                    onChange={(event) => selection.onToggle(item.id, event.target.checked)}
                  />
                </td>
              )}
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
              <td className={styles.actions}>
                <PreviewDialog upload={item} onUnauthorized={onUnauthorized} />
                <DownloadButton upload={item} onUnauthorized={onUnauthorized} />
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
