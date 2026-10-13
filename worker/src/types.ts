export const VERSION = 1 as const;
export type TradeState = "not_sent" | "created_pending_confirmation" | "confirmed" |
  "sent" | "failed" | "unknown_outcome";
export type InventorySpec = { app_id: number; context_id: string };
export type Asset = InventorySpec & { asset_id: string; amount: number };
export type Credentials = {
  account_name: string; password: string; shared_secret: string; identity_secret?: string;
  refresh_token?: string; proxy_url?: string;
};
export type InventoryFailure = InventorySpec & { code: string };
export type InventoryResult = {
  assets: Asset[]; failures: InventoryFailure[]; simulation: boolean;
};
export type TradeResult = {
  operation_id: string; account_id: string; state: TradeState; offer_id: string | null;
  code: string | null; updated_at_ms: number; simulation: boolean;
};
export type ActivityState = "not_started" | "warming_up" | "playing" | "completed" |
  "cancelled" | "failed" | "unknown_outcome";
export type ActivityResult = {
  operation_id: string; account_id: string; state: ActivityState; granted_app_ids: number[];
  code: string | null; updated_at_ms: number; simulation: boolean;
};
export type Params = {
  account_id?: string; operation_id?: string; trade_url?: string;
  inventories?: InventorySpec[]; credentials?: Credentials; allow_partial?: boolean;
  target_request_id?: string;
  selected_assets?: Asset[]; comment?: string;
  app_ids?: number[]; duration_ms?: number; machine_name?: string;
};
export type Method = "inventory" | "send" | "confirm" | "reconcile" | "status" | "cancel" |
  "renew" | "activity" | "activity_status";
export type Request = {
  version: 1; request_id: string; method: Method; timeout_ms: number; params: Params;
  admission?: { expires_at_ms: number };
};
export type Response = {
  version: 1; request_id: string | null; ok: boolean;
  result?: TradeResult | InventoryResult | ActivityResult | { cancelled: boolean } | { renewed: boolean };
  error?: { code: string };
};
export class WorkerError extends Error {
  readonly code: string;
  constructor(code: string) { super(code); this.code = code; }
}
const codes = new Set([
  "invalid_request", "protocol_version", "invalid_json", "frame_too_large", "worker_busy",
  "duplicate_request", "request_limit", "admission_required", "admission_expired", "cancelled",
  "deadline_exceeded", "operation_not_found", "operation_conflict", "confirmation_unavailable",
  "no_items", "inventory_failed", "inventory_limit", "pagination_loop", "invalid_inventory",
  "partial_inventory", "auth_failed", "interactive_auth_required", "cookies_failed",
  "transport_lost", "rate_limited", "send_unknown", "confirmation_unknown", "status_unavailable",
  "live_stand_required", "internal_error", "journal_error", "recovered_after_restart",
  "unresolved_operation",
]);
export function safeCode(error: unknown, fallback = "internal_error"): string {
  return error instanceof WorkerError && codes.has(error.code) ? error.code : fallback;
}
export function checkpoint(signal: AbortSignal): void {
  if (signal.aborted) throw signal.reason instanceof WorkerError ? signal.reason :
    new WorkerError("cancelled");
}
export function admission(request: Request, now = Date.now()): void {
  const until = request.admission?.expires_at_ms;
  if (!until) throw new WorkerError("admission_required");
  if (until <= now || until > now + 60_000) throw new WorkerError("admission_expired");
}
export interface Transport {
  readonly simulation: boolean;
  connect(credentials: Credentials, signal: AbortSignal, machineName?: string): Promise<void>;
  inventory(spec: InventorySpec, cursor: string | undefined, signal: AbortSignal):
    Promise<{ assets: Asset[]; next?: string }>;
  send(url: string, assets: Asset[], signal: AbortSignal, comment?: string):
    Promise<{ offer_id: string; status: "sent" | "pending" }>;
  confirm(offerId: string, identitySecret: string, signal: AbortSignal): Promise<void>;
  reconcile(offerId: string, partnerId: string, signal: AbortSignal):
    Promise<"sent" | "created_pending_confirmation" | "failed" | "unknown_outcome">;
  close(): void;
  freeLicenses(appIds: number[], signal: AbortSignal): Promise<number[]>;
  startActivity(appIds: number[]): void;
  stopActivity(): void;
  activityDelay(ms: number, signal: AbortSignal): Promise<void>;
}
