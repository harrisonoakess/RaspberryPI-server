/** The volume browser: totals, filters, and either a flat file list or per-card groups.
 *
 * Sorting and filtering are the server's job, so what the table shows is the
 * whole stored set narrowed — never just the rows a previous page happened to
 * have fetched. That is also why the query is one piece of state: changing a
 * filter or a sort has to reset the page in the same update, or the first
 * request after the change would ask for a page that no longer exists.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  NO_FILTERS,
  UnauthorizedError,
  getUploadSummary,
  listUploads,
  type SortKey,
  type SortOrder,
  type UploadFilters,
  type UploadPage,
  type UploadSummary,
} from "../api";
import { CardGroups } from "./CardGroups";
import { UploadsSummaryBar } from "./UploadsSummary";
import { UploadsTable, defaultOrderFor } from "./UploadsTable";
import { UploadsToolbar, type ViewMode } from "./UploadsToolbar";
import styles from "./UploadsPanel.module.css";

const PAGE_SIZE = 50;

/** Long enough that ordinary typing sends one request, short enough to feel live. */
const SEARCH_DEBOUNCE_MS = 250;

interface UploadsQuery {
  filters: UploadFilters;
  sort: SortKey;
  order: SortOrder;
  offset: number;
}

const INITIAL_QUERY: UploadsQuery = {
  filters: NO_FILTERS,
  sort: "received_at",
  order: "desc",
  offset: 0,
};

interface UploadsPanelProps {
  reloadToken: number;
  onUnauthorized: () => void;
}

export function UploadsPanel({ reloadToken, onUnauthorized }: UploadsPanelProps) {
  const [view, setView] = useState<ViewMode>("files");
  const [query, setQuery] = useState<UploadsQuery>(INITIAL_QUERY);

  // The text box updates on every keystroke; `query.filters.q` lags behind it by
  // the debounce, and is the value actually sent.
  const [searchText, setSearchText] = useState("");

  const [page, setPage] = useState<UploadPage | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);
  const [pageLoading, setPageLoading] = useState(true);

  const [summary, setSummary] = useState<UploadSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);

  const { filters, sort, order, offset } = query;
  const filtered = filters.q !== "" || filters.cardUuid !== "" || filters.deviceId !== "";

  useEffect(() => {
    const timer = setTimeout(() => {
      setQuery((current) =>
        current.filters.q === searchText
          ? current
          : { ...current, filters: { ...current.filters, q: searchText }, offset: 0 },
      );
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchText]);

  // Totals are wanted in both views: the flat list shows them above the table,
  // and the grouped view is built from the same response's card rollups.
  useEffect(() => {
    let cancelled = false;
    setSummaryError(null);
    void (async () => {
      try {
        const next = await getUploadSummary(filters);
        if (!cancelled) {
          setSummary(next);
        }
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof UnauthorizedError) {
          onUnauthorized();
          return;
        }
        setSummaryError("Could not load upload totals.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [filters, reloadToken, onUnauthorized]);

  useEffect(() => {
    if (view !== "files") {
      return;
    }
    let cancelled = false;
    setPageLoading(true);
    setPageError(null);
    void (async () => {
      try {
        const next = await listUploads({ ...filters, sort, order, limit: PAGE_SIZE, offset });
        if (!cancelled) {
          setPage(next);
        }
      } catch (caught) {
        if (cancelled) return;
        if (caught instanceof UnauthorizedError) {
          onUnauthorized();
          return;
        }
        setPageError("Could not load uploads.");
      } finally {
        if (!cancelled) {
          setPageLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [filters, sort, order, offset, view, reloadToken, onUnauthorized]);

  // A refresh must not leave a stale page of a filter that no longer applies.
  useEffect(() => {
    setPage(null);
  }, [reloadToken]);

  const handleFilter = useCallback((patch: Partial<UploadFilters>) => {
    setQuery((current) => ({
      ...current,
      filters: { ...current.filters, ...patch },
      offset: 0,
    }));
  }, []);

  const handleClear = useCallback(() => {
    setSearchText("");
    setQuery((current) => ({ ...current, filters: NO_FILTERS, offset: 0 }));
  }, []);

  const handleSort = useCallback((key: SortKey) => {
    setQuery((current) => ({
      ...current,
      sort: key,
      // Re-clicking the active column reverses it; a new column starts in the
      // direction that column is usually read in.
      order:
        current.sort === key
          ? current.order === "asc"
            ? "desc"
            : "asc"
          : defaultOrderFor(key),
      offset: 0,
    }));
  }, []);

  const goTo = useCallback((nextOffset: number) => {
    setQuery((current) => ({ ...current, offset: Math.max(0, nextOffset) }));
  }, []);

  const caption = useMemo(() => {
    if (page === null || page.total === 0) {
      return "Stored uploads.";
    }
    const first = page.offset + 1;
    const last = page.offset + page.items.length;
    return `Stored uploads ${first}–${last} of ${page.total}.`;
  }, [page]);

  const empty = page !== null && page.total === 0 && !pageLoading;

  return (
    <section className={styles.panel} aria-labelledby="uploads-heading">
      <h2 id="uploads-heading" className={styles.heading}>
        Uploads
      </h2>

      <UploadsSummaryBar summary={summary} filtered={filtered} error={summaryError} />

      <UploadsToolbar
        searchText={searchText}
        onSearchText={setSearchText}
        filters={filters}
        onFilter={handleFilter}
        onClear={handleClear}
        cardOptions={summary?.all_card_uuids ?? []}
        deviceOptions={summary?.all_device_ids ?? []}
        view={view}
        onView={setView}
        filtered={filtered}
      />

      {view === "cards" ? (
        summary === null ? (
          <p className={styles.state}>Loading cards…</p>
        ) : (
          <CardGroups
            summary={summary}
            search={filters.q}
            sort={sort}
            order={order}
            onSort={handleSort}
            onUnauthorized={onUnauthorized}
          />
        )
      ) : (
        <>
          {pageError !== null && (
            <p className={styles.error} role="alert">
              {pageError}
            </p>
          )}

          {pageLoading && page === null && pageError === null && (
            <p className={styles.state}>Loading uploads…</p>
          )}

          {empty && pageError === null && (
            <p className={styles.state}>
              {filtered
                ? "No uploads match the current filters."
                : "No uploads have been stored yet."}
            </p>
          )}

          {page !== null && page.items.length > 0 && (
            <>
              <UploadsTable
                items={page.items}
                sort={sort}
                order={order}
                onSort={handleSort}
                onUnauthorized={onUnauthorized}
                caption={caption}
              />
              <Pager page={page} loading={pageLoading} onGoTo={goTo} />
            </>
          )}
        </>
      )}
    </section>
  );
}

interface PagerProps {
  page: UploadPage;
  loading: boolean;
  onGoTo: (offset: number) => void;
}

function Pager({ page, loading, onGoTo }: PagerProps) {
  const hasPrevious = page.offset > 0;
  const hasNext = page.offset + page.items.length < page.total;
  if (!hasPrevious && !hasNext) {
    return null;
  }

  const pageNumber = Math.floor(page.offset / page.limit) + 1;
  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));

  return (
    <nav className={styles.pager} aria-label="Upload pages">
      <button
        type="button"
        className={styles.pageButton}
        onClick={() => onGoTo(page.offset - page.limit)}
        disabled={!hasPrevious || loading}
      >
        Previous
      </button>
      <span className={styles.pageStatus}>
        Page {pageNumber} of {pageCount}
      </span>
      <button
        type="button"
        className={styles.pageButton}
        onClick={() => onGoTo(page.offset + page.limit)}
        disabled={!hasNext || loading}
      >
        Next
      </button>
    </nav>
  );
}
