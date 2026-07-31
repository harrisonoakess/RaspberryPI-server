/** Uploads rolled up per card, each group expanding to the files it holds.
 *
 * A group's own files are fetched only when it is opened: a dashboard with many
 * cards would otherwise load every file on the volume to show four numbers.
 */

import { useCallback, useEffect, useState } from "react";

import {
  UnauthorizedError,
  listUploads,
  type CardSummary,
  type SortKey,
  type SortOrder,
  type UploadItem,
  type UploadSummary,
} from "../api";
import { formatBytes } from "../format";
import { Timestamp } from "./Timestamp";
import { UploadsTable } from "./UploadsTable";
import styles from "./CardGroups.module.css";

/** The server's ceiling for one page; a group past it says so rather than lying. */
const GROUP_FILE_LIMIT = 100;

/** Groups are keyed by device and card together, which is what makes a card unique. */
function keyOf(card: Pick<CardSummary, "device_id" | "card_uuid">): string {
  return `${card.device_id}/${card.card_uuid}`;
}

interface CardGroupsProps {
  summary: UploadSummary;
  /** The active filename search, so an opened group lists the same files it counted. */
  search: string;
  sort: SortKey;
  order: SortOrder;
  onSort: (key: SortKey) => void;
  onUnauthorized: () => void;
}

export function CardGroups({
  summary,
  search,
  sort,
  order,
  onSort,
  onUnauthorized,
}: CardGroupsProps) {
  const [expanded, setExpanded] = useState<string | null>(null);

  if (summary.cards.length === 0) {
    return <p className={styles.state}>No cards match the current filters.</p>;
  }

  return (
    <div className={styles.groups}>
      {summary.cards.map((card) => {
        const key = keyOf(card);
        const open = expanded === key;
        return (
          <section key={key} className={styles.group}>
            <h3 className={styles.heading}>
              <button
                type="button"
                className={styles.toggle}
                aria-expanded={open}
                onClick={() => setExpanded(open ? null : key)}
              >
                <span aria-hidden="true" className={styles.chevron}>
                  {open ? "▾" : "▸"}
                </span>
                <span className={styles.cardUuid}>{card.card_uuid}</span>
                <span className={styles.device}>{card.device_id}</span>
                <span className={styles.counts}>
                  {card.file_count} {card.file_count === 1 ? "file" : "files"} ·{" "}
                  {formatBytes(card.total_bytes)}
                </span>
                <span className={styles.latest}>
                  <Timestamp utc={card.newest_received_at} />
                </span>
              </button>
            </h3>

            {open && (
              <CardFiles card={card} search={search} sort={sort} order={order} onSort={onSort} onUnauthorized={onUnauthorized} />
            )}
          </section>
        );
      })}

      {summary.cards_truncated && (
        <p className={styles.state}>
          Only the {summary.cards.length} most recently active cards are shown. Filter by
          device to see the rest.
        </p>
      )}
    </div>
  );
}

interface CardFilesProps {
  card: CardSummary;
  search: string;
  sort: SortKey;
  order: SortOrder;
  onSort: (key: SortKey) => void;
  onUnauthorized: () => void;
}

function CardFiles({ card, search, sort, order, onSort, onUnauthorized }: CardFilesProps) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const cardUuid = card.card_uuid;
  const deviceId = card.device_id;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await listUploads({
        q: search,
        cardUuid,
        deviceId,
        sort,
        order,
        limit: GROUP_FILE_LIMIT,
        offset: 0,
      });
      setItems(page.items);
      setTotal(page.total);
    } catch (caught) {
      if (caught instanceof UnauthorizedError) {
        onUnauthorized();
        return;
      }
      setError("Could not load this card's files.");
    } finally {
      setLoading(false);
    }
  }, [cardUuid, deviceId, search, sort, order, onUnauthorized]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error !== null) {
    return (
      <p className={styles.error} role="alert">
        {error}
      </p>
    );
  }

  if (loading && items.length === 0) {
    return <p className={styles.state}>Loading files…</p>;
  }

  const shown = items.length;
  const caption =
    total > shown
      ? `${shown} of ${total} files on ${cardUuid}. Switch to the Files view to page through the rest.`
      : `${shown} ${shown === 1 ? "file" : "files"} on ${cardUuid}.`;

  return (
    <div className={styles.files}>
      <UploadsTable
        items={items}
        sort={sort}
        order={order}
        onSort={onSort}
        onUnauthorized={onUnauthorized}
        caption={caption}
        hidden={["card_uuid", "device_id"]}
      />
    </div>
  );
}
