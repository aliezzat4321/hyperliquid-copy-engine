export interface FeedPage {
  items?: any[];
}

export interface FeedBackfill {
  posts: any[];
  newestPostId: string | null;
  savedCursor: string | null;
  cursorReached: boolean;
  pagesFetched: number;
  exhausted: boolean;
}

/**
 * Fetch newest-to-oldest until the prior durable high-water mark is reached.
 * `onPage` fires after each page is fetched so a caller-owned liveness watchdog can be
 * beaten on real progress instead of only once the full (potentially many-page)
 * backfill completes; that keeps the watchdog bound independent of `maxPages`.
 */
export async function fetchFeedBackfill(
  fetchPage: (lastPostId: string | null) => Promise<FeedPage>,
  savedCursor: string | null,
  maxPages: number,
  onPage?: () => void,
): Promise<FeedBackfill> {
  const posts: any[] = [];
  const collectedPostIds = new Set<string>();
  const pageTokens = new Set<string>();
  let lastPostId: string | null = null;
  let newestPostId: string | null = null;
  let cursorReached = savedCursor == null;
  let exhausted = false;
  let pagesFetched = 0;

  while (pagesFetched < maxPages) {
    const page = await fetchPage(lastPostId);
    pagesFetched += 1;
    onPage?.();
    const items = Array.isArray(page?.items) ? page.items : [];
    if (!items.length) { exhausted = true; break; }
    if (newestPostId == null) newestPostId = String(items[0]?.id ?? '') || null;

    for (const post of items) {
      const postId = String(post?.id ?? '');
      if (savedCursor != null && postId === savedCursor) {
        cursorReached = true;
        break;
      }
      if (!postId || !collectedPostIds.has(postId)) posts.push(post);
      if (postId) collectedPostIds.add(postId);
    }
    if (cursorReached) break;

    const next = String(items[items.length - 1]?.id ?? '');
    if (!next || pageTokens.has(next)) { exhausted = true; break; }
    pageTokens.add(next);
    lastPostId = next;
  }

  return { posts, newestPostId, savedCursor, cursorReached, pagesFetched, exhausted };
}
