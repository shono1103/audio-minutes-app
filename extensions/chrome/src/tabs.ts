/** タブ一覧のフィルタ。meet.google.com の https タブだけを返し、ページ URL を含めない。 */

import type { TabSummary } from "./messages";

export const MEET_HOST = "meet.google.com";

export interface TabLike {
  id?: number;
  windowId?: number;
  title?: string;
  url?: string;
  pendingUrl?: string;
  favIconUrl?: string;
  audible?: boolean;
}

export function isMeetUrl(url: string | undefined): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === MEET_HOST;
  } catch {
    return false;
  }
}

function safeFavicon(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

/** Meet タブなら要約を返し、それ以外は null。要約にページ URL は入れない。 */
export function toTabSummary(tab: TabLike): TabSummary | null {
  if (tab.id === undefined || tab.windowId === undefined) return null;
  if (!isMeetUrl(tab.url ?? tab.pendingUrl)) return null;
  return {
    tab_id: tab.id,
    title: tab.title ?? "",
    favicon_url: safeFavicon(tab.favIconUrl),
    audible: tab.audible === true,
    window_id: tab.windowId,
  };
}

export function summarizeMeetTabs(tabs: TabLike[]): TabSummary[] {
  return tabs
    .map(toTabSummary)
    .filter((summary): summary is TabSummary => summary !== null)
    .sort((a, b) => a.tab_id - b.tab_id);
}
