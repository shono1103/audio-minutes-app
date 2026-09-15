import { describe, expect, it } from "vitest";

import { isMeetUrl, summarizeMeetTabs, toTabSummary } from "../src/tabs";

describe("isMeetUrl", () => {
  it("https の meet.google.com だけを対象にする", () => {
    expect(isMeetUrl("https://meet.google.com/abc-defg-hij")).toBe(true);
    expect(isMeetUrl("https://meet.google.com/")).toBe(true);
    expect(isMeetUrl("http://meet.google.com/abc")).toBe(false);
    expect(isMeetUrl("https://meet.google.com.evil.example/abc")).toBe(false);
    expect(isMeetUrl("https://www.google.com/")).toBe(false);
    expect(isMeetUrl("https://zoom.us/j/1")).toBe(false);
    expect(isMeetUrl(undefined)).toBe(false);
    expect(isMeetUrl("not a url")).toBe(false);
  });
});

describe("toTabSummary", () => {
  it("要約に完全な URL を含めない", () => {
    const summary = toTabSummary({
      id: 7,
      windowId: 1,
      title: "週次MTG - Google Meet",
      url: "https://meet.google.com/abc-defg-hij?authuser=1#secret",
      favIconUrl: "https://fonts.gstatic.com/s/i/productlogos/meet_2020q4/v1/web-96dp/logo_meet_2020q4_color_2x_web_96dp.png",
      audible: true,
    });
    expect(summary).not.toBeNull();
    expect(summary).toEqual({
      tab_id: 7,
      title: "週次MTG - Google Meet",
      favicon_url:
        "https://fonts.gstatic.com/s/i/productlogos/meet_2020q4/v1/web-96dp/logo_meet_2020q4_color_2x_web_96dp.png",
      audible: true,
      window_id: 1,
    });
    expect(JSON.stringify(summary)).not.toContain("abc-defg-hij");
    expect(JSON.stringify(summary)).not.toContain("authuser");
  });

  it("http のファビコンは落とす", () => {
    const summary = toTabSummary({ id: 1, windowId: 1, url: "https://meet.google.com/x", favIconUrl: "http://x/f.png" });
    expect(summary?.favicon_url).toBeNull();
  });

  it("Meet 以外のタブは null", () => {
    expect(toTabSummary({ id: 1, windowId: 1, url: "https://zoom.us/" })).toBeNull();
    expect(toTabSummary({ id: 1, windowId: 1, url: "https://docs.google.com/" })).toBeNull();
    expect(toTabSummary({ windowId: 1, url: "https://meet.google.com/" })).toBeNull();
  });
});

describe("summarizeMeetTabs", () => {
  it("一般タブを除外し tab_id 順に返す", () => {
    const summaries = summarizeMeetTabs([
      { id: 3, windowId: 1, url: "https://meet.google.com/aaa", title: "A" },
      { id: 1, windowId: 1, url: "https://example.com/", title: "B" },
      { id: 2, windowId: 2, url: "https://meet.google.com/ccc", title: "C", audible: true },
    ]);
    expect(summaries.map((tab) => tab.tab_id)).toEqual([2, 3]);
    expect(summaries.every((tab) => !("url" in tab))).toBe(true);
  });
});
