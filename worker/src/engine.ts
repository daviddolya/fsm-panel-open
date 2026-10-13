import { Journal } from "./journal.ts";
import { collectInventory } from "./inventory.ts";
import { admission, checkpoint, safeCode, VERSION, WorkerError } from "./types.ts";
import type { Request, Response, TradeResult, Transport } from "./types.ts";

export class Engine {
  private journal: Journal;
  private factory: () => Transport;
  private active = new Map<string, AbortController>();
  private seen = new Set<string>();
  constructor(journal: Journal, factory: () => Transport) {
    this.journal = journal; this.factory = factory;
  }
  async handle(request: Request): Promise<Response> {
    const response = (data: Partial<Response>): Response =>
      ({ version: VERSION, request_id: request.request_id, ok: false, ...data });
    if (this.seen.has(request.request_id)) return response({ error: { code: "duplicate_request" } });
    if (this.seen.size >= 100_000) return response({ error: { code: "request_limit" } });
    this.seen.add(request.request_id);
    if (request.method === "cancel") {
      const controller = this.active.get(request.params.target_request_id!);
      controller?.abort(new WorkerError("cancelled"));
      return response({ ok: true, result: { cancelled: Boolean(controller) } });
    }
    if (this.active.size) return response({ error: { code: "worker_busy" } });
    const controller = new AbortController();
    this.active.set(request.request_id, controller);
    const timer = setTimeout(() => controller.abort(new WorkerError("deadline_exceeded")),
      request.timeout_ms);
    let transport: Transport | undefined;
    let ownsReservation = false;
    try {
      const p = request.params;
      if (request.method === "status") {
        return response({ ok: true, result: this.journal.public(
          this.journal.get(p.operation_id!, p.account_id!)) });
      }
      if (request.method === "send" || request.method === "inventory") admission(request);
      if (request.method === "send") {
        const reserved = this.journal.reserve(request);
        if (!reserved.created) return response({ ok: true, result: this.journal.public(reserved.row) });
        ownsReservation = true;
      }
      if (request.method === "confirm" || request.method === "reconcile") {
        const row = this.journal.get(p.operation_id!, p.account_id!);
        if (request.method === "confirm" && row.state !== "created_pending_confirmation") {
          return response({ ok: true, result: this.journal.public(row) });
        }
        // With no known offer ID, a scan cannot establish absence or safe retry.
        if (!row.offer_id) return response({ ok: true, result: this.journal.public(row) });
      }
      transport = this.factory();
      await transport.connect(p.credentials!, controller.signal);
      checkpoint(controller.signal);
      const result = await this.execute(request, transport, controller.signal);
      return response({ ok: true, result });
    } catch (error) {
      if (request.method === "send" && ownsReservation) {
        try {
          const row = this.journal.get(request.params.operation_id!, request.params.account_id!);
          if (row.state === "not_sent") {
            const code = safeCode(error);
            const state = ["cancelled", "deadline_exceeded", "admission_expired"].includes(code)
              ? "not_sent" : "failed";
            return response({ ok: true, result: this.journal.update(
              row.operation_id, row.account_id, state, code) });
          }
        } catch { /* The primary error remains sanitized below. */ }
      }
      return response({ error: { code: safeCode(error) } });
    } finally {
      clearTimeout(timer);
      try { transport?.close(); } catch { /* Never emit an SDK error. */ }
      this.active.delete(request.request_id);
    }
  }
  private async execute(request: Request, transport: Transport, signal: AbortSignal):
    Promise<TradeResult | Awaited<ReturnType<typeof collectInventory>>> {
    const p = request.params;
    if (request.method === "inventory") return collectInventory(transport, p.inventories!, signal);
    const id = p.operation_id!, account = p.account_id!;
    if (request.method === "send") {
      const inventory = await collectInventory(transport, p.inventories!, signal);
      if (inventory.failures.length && p.allow_partial === false) {
        return this.journal.update(id, account, "failed", "partial_inventory");
      }
      if (!inventory.assets.length) return this.journal.update(id, account, "failed",
        inventory.failures.length ? "inventory_failed" : "no_items");
      checkpoint(signal);
      admission(request);
      // Durable UNKNOWN before the first possible send. No exception permits retry.
      this.journal.claim(id, account, "not_sent", "sending");
      try {
        const sent = await transport.send(p.trade_url!, inventory.assets, signal);
        checkpoint(signal);
        if (!/^[1-9][0-9]{0,19}$/.test(sent.offer_id) ||
            !["sent", "pending"].includes(sent.status)) throw new WorkerError("send_unknown");
        return this.journal.update(id, account,
          sent.status === "pending" ? "created_pending_confirmation" : "sent",
          inventory.failures.length ? "partial_inventory" : null, sent.offer_id);
      } catch (error) {
        return this.journal.update(id, account, "unknown_outcome", safeCode(error, "send_unknown"));
      }
    }
    const row = this.journal.get(id, account);
    if (request.method === "confirm") {
      if (!p.credentials?.identity_secret) throw new WorkerError("confirmation_unavailable");
      checkpoint(signal);
      this.journal.claim(id, account, "created_pending_confirmation", "confirming");
      try {
        await transport.confirm(row.offer_id!, p.credentials.identity_secret, signal);
        checkpoint(signal);
        return this.journal.update(id, account, "confirmed");
      } catch (error) {
        return this.journal.update(id, account, "unknown_outcome",
          safeCode(error, "confirmation_unknown"));
      }
    }
    try {
      const state = await transport.reconcile(row.offer_id!, row.partner_id, signal);
      checkpoint(signal);
      if (state === "unknown_outcome") return this.journal.public(row);
      return this.journal.update(id, account, state);
    } catch {
      // Read failure is not proof of send failure and must not clear uncertainty.
      return this.journal.public(row);
    }
  }
  stop(): void {
    for (const controller of this.active.values()) controller.abort(new WorkerError("cancelled"));
  }
}
