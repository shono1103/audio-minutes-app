export interface OffscreenStateResponse {
  active_capture_id: string | null;
}

/** 保存値だけを信頼せず、実際のoffscreen captureと一致した場合だけ復元する。 */
export function restoredCaptureMatches(storedCaptureId: string, response: unknown): boolean {
  if (typeof response !== "object" || response === null) return false;
  return (response as Partial<OffscreenStateResponse>).active_capture_id === storedCaptureId;
}
