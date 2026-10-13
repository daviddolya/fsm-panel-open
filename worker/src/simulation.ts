import { setTimeout as delay } from "node:timers/promises";
import { checkpoint, WorkerError } from "./types.ts";
import type { Asset, Credentials, InventorySpec, Transport } from "./types.ts";

export type Scenario = {
  pages?: Record<string, { assets?: Asset[]; next?: string; error?: boolean }[]>;
  auth_error?: boolean; send_error?: boolean; confirm_error?: boolean;
  delay_ms?: number; send_delay_ms?: number; send_status?: "pending" | "sent";
  reconcile_state?: "sent" | "created_pending_confirmation" | "failed" | "unknown_outcome";
};
export class SimulationTransport implements Transport {
  readonly simulation = true;
  sendCount = 0;
  confirmCount = 0;
  private scenario: Scenario;
  constructor(scenario: Scenario = {}) { this.scenario = scenario; }
  private async pause(signal: AbortSignal, ms = this.scenario.delay_ms ?? 0) {
    checkpoint(signal);
    if (ms) {
      try { await delay(Math.min(Math.max(ms, 0), 300_000), undefined, { signal }); }
      catch { checkpoint(signal); }
    }
    checkpoint(signal);
  }
  async connect(_credentials: Credentials, signal: AbortSignal): Promise<void> {
    await this.pause(signal);
    if (this.scenario.auth_error) throw new WorkerError("auth_failed");
  }
  async inventory(spec: InventorySpec, cursor: string | undefined, signal: AbortSignal) {
    await this.pause(signal);
    const index = cursor === undefined ? 0 : Number(cursor);
    const pages = this.scenario.pages?.[`${spec.app_id}/${spec.context_id}`] ?? [];
    const page = pages[index];
    if (page?.error) throw new WorkerError("inventory_failed");
    return { assets: page?.assets ?? [], next: page?.next ??
      (index + 1 < pages.length ? String(index + 1) : undefined) };
  }
  async send(_url: string, _assets: Asset[], signal: AbortSignal) {
    this.sendCount++;
    await this.pause(signal, this.scenario.send_delay_ms ?? 0);
    if (this.scenario.send_error) throw new WorkerError("transport_lost");
    return { offer_id: "900000001", status: this.scenario.send_status ?? "sent" as const };
  }
  async confirm(_id: string, _secret: string, signal: AbortSignal): Promise<void> {
    this.confirmCount++;
    await this.pause(signal);
    if (this.scenario.confirm_error) throw new WorkerError("transport_lost");
  }
  async reconcile(_id: string, _partnerId: string, signal: AbortSignal) {
    await this.pause(signal);
    return this.scenario.reconcile_state ?? "unknown_outcome" as const;
  }
  close(): void { /* no network or persistent resources */ }
}
