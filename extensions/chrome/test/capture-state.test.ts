import { describe, expect, it } from "vitest";

import { restoredCaptureMatches } from "../src/capture-state";

describe("restoredCaptureMatches", () => {
  it("offscreenの実captureが一致するときだけ復元する", () => {
    expect(restoredCaptureMatches("capture-a", { active_capture_id: "capture-a" })).toBe(true);
    expect(restoredCaptureMatches("capture-a", { active_capture_id: "capture-b" })).toBe(false);
    expect(restoredCaptureMatches("capture-a", { active_capture_id: null })).toBe(false);
    expect(restoredCaptureMatches("capture-a", undefined)).toBe(false);
  });
});
