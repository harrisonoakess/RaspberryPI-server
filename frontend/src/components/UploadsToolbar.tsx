/** Search, card and device filters, and the flat/grouped view switch. */

import type { UploadFilters } from "../api";
import styles from "./UploadsToolbar.module.css";

export type ViewMode = "files" | "cards";

interface UploadsToolbarProps {
  searchText: string;
  onSearchText: (value: string) => void;
  filters: UploadFilters;
  onFilter: (patch: Partial<UploadFilters>) => void;
  onClear: () => void;
  /** Every stored value, so a filter can always be widened again. */
  cardOptions: readonly string[];
  deviceOptions: readonly string[];
  view: ViewMode;
  onView: (view: ViewMode) => void;
  filtered: boolean;
}

const VIEWS: readonly { value: ViewMode; label: string }[] = [
  { value: "files", label: "Files" },
  { value: "cards", label: "By card" },
];

export function UploadsToolbar({
  searchText,
  onSearchText,
  filters,
  onFilter,
  onClear,
  cardOptions,
  deviceOptions,
  view,
  onView,
  filtered,
}: UploadsToolbarProps) {
  return (
    <div className={styles.toolbar}>
      <div className={styles.field}>
        <label className={styles.label} htmlFor="uploads-search">
          Search filenames
        </label>
        <input
          id="uploads-search"
          className={styles.input}
          type="search"
          value={searchText}
          placeholder="logger-0001"
          onChange={(event) => onSearchText(event.target.value)}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="uploads-card">
          Card
        </label>
        <select
          id="uploads-card"
          className={styles.select}
          value={filters.cardUuid}
          onChange={(event) => onFilter({ cardUuid: event.target.value })}
        >
          <option value="">All cards</option>
          {cardOptions.map((card) => (
            <option key={card} value={card}>
              {card}
            </option>
          ))}
        </select>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor="uploads-device">
          Device
        </label>
        <select
          id="uploads-device"
          className={styles.select}
          value={filters.deviceId}
          onChange={(event) => onFilter({ deviceId: event.target.value })}
        >
          <option value="">All devices</option>
          {deviceOptions.map((device) => (
            <option key={device} value={device}>
              {device}
            </option>
          ))}
        </select>
      </div>

      <div className={styles.trailing}>
        <div className={styles.viewSwitch} role="group" aria-label="View mode">
          {VIEWS.map((option) => (
            <button
              key={option.value}
              type="button"
              className={
                option.value === view ? `${styles.viewButton} ${styles.active}` : styles.viewButton
              }
              aria-pressed={option.value === view}
              onClick={() => onView(option.value)}
            >
              {option.label}
            </button>
          ))}
        </div>

        {/* Only offered when there is something to clear, so the control never
            promises an effect it would not have. */}
        {filtered && (
          <button type="button" className={styles.clear} onClick={onClear}>
            Clear filters
          </button>
        )}
      </div>
    </div>
  );
}
