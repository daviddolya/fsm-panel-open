import { Journal } from "./journal.ts";
import { collectInventory } from "./inventory.ts";
import { admission, checkpoint, safeCode, VERSION, WorkerError } from "./types.ts";
import type { ActivityResult, Request, Response, TradeResult, Transport } from "./types.ts";

export class Engine {
  private journal: Journal;
  private factory: () => Transport;
  private active = new Map<string, AbortController>();
  private seen = new Set<string>();
  private admitted = new Map<string, { request: Request; timer: ReturnType<typeof setTimeout> }>();
  constructor(journal: Journal, factory: () => Transport) {
    this.journal = journal; this.factory = factory;
  }
  private armAdmission(request: Request, controller: AbortController): void {
    const previous = this.admitted.get(request.request_id);
    if (previous) clearTimeout(previous.timer);
    const timer = setTimeout(() => controller.abort(new WorkerError("admission_expired")),
      Math.max(0, request.admission!.expires_at_ms - Date.now()));
    this.admitted.set(request.request_id, { request, timer });
  }
  async handle(request: Request): Promise<Response> {
    const response = (data: Partial<Response>): Response =>
      ({ version: VERSION, request_id: request.request_id, ok: false, ...data });
    if (this.seen.has(request.request_id)) return response({ error: { code: "duplicate_request" } });
    if (this.seen.size >= 100_000) return response({ error: { code: "request_limit" } });
    this.seen.add(request.request_id);
    if (request.method === "renew") {
      try {
        admission(request);
        const existing = this.admitted.get(request.params.target_request_id!);
        const controller = this.active.get(request.params.target_request_id!);
        if (!existing || !controller) return response({ ok: true, result: { renewed: false } });
        existing.request.admission = request.admission;
        this.armAdmission(existing.request, controller);
        return response({ ok: true, result: { renewed: true } });
      } catch (error) { return response({ error: { code: safeCode(error) } }); }
    }
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
      if (request.method === "activity_status") {
        return response({ ok: true, result: this.journal.getActivity(p.operation_id!,p.account_id!) });
      }
      if (request.method === "status") {
        return response({ ok: true, result: this.journal.public(
          this.journal.get(p.operation_id!, p.account_id!)) });
      }
      if (["send", "inventory", "activity"].includes(request.method)) {
        admission(request);
        this.armAdmission(request, controller);
      }
      if (request.method === "send") {
        const reserved = this.journal.reserve(request);
        if (!reserved.created) return response({ ok: true, result: this.journal.public(reserved.row) });
        ownsReservation = true;
      }
      if (request.method === "activity") {
        const reserved = this.journal.reserveActivity(request);
        if (!reserved.created) return response({ ok: true, result: reserved.row });
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
      await transport.connect(p.credentials!, controller.signal, p.machine_name);
      checkpoint(controller.signal);
      const result = await this.execute(request, transport, controller.signal);
      return response({ ok: true, result });
    } catch (error) {
      if (request.method === "activity" && ownsReservation) {
        const row = this.journal.getActivity(request.params.operation_id!,request.params.account_id!);
        if (row.state === "not_started") return response({ ok: true, result:
          this.journal.updateActivity(row.operation_id,row.account_id,"failed",safeCode(error)) });
      }
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
      const grant = this.admitted.get(request.request_id);
      if (grant) clearTimeout(grant.timer);
      this.admitted.delete(request.request_id);
      try { transport?.close(); } catch { /* Never emit an SDK error. */ }
      this.active.delete(request.request_id);
    }
  }
  private async execute(request: Request, transport: Transport, signal: AbortSignal):
    Promise<TradeResult | ActivityResult | Awaited<ReturnType<typeof collectInventory>>> {
    const p = request.params;
    if (request.method === "inventory") return collectInventory(transport, p.inventories!, signal);
    const id = p.operation_id!, account = p.account_id!;
    if (request.method === "activity") {
      let licencesKnown = false;
      try {
        checkpoint(signal); admission(request);
        this.journal.updateActivity(id,account,"unknown_outcome");
        const granted = await transport.freeLicenses([...new Set(p.app_ids)], signal);
        checkpoint(signal);
        licencesKnown = true;
        this.journal.updateActivity(id,account,"warming_up",null,granted);
        await transport.activityDelay(80_000, signal);
        checkpoint(signal); admission(request);
        this.journal.updateActivity(id,account,"playing");
        transport.startActivity([...new Set(p.app_ids)]);
        await transport.activityDelay(p.duration_ms!, signal);
        checkpoint(signal);
        return this.journal.updateActivity(id,account,"completed");
      } catch (error) {
        return this.journal.updateActivity(id,account,licencesKnown ? "cancelled" : "unknown_outcome",safeCode(error));
      } finally {
        try { transport.stopActivity(); } catch { /* Logoff follows in close. */ }
      }
    }
    if (request.method === "send") {
      const selected = new Map<string, (NonNullable<typeof p.selected_assets>)[number]>();
      for (const item of p.selected_assets ?? []) {
        const key = `${item.app_id}/${item.context_id}/${item.asset_id}`;
        if (selected.has(key) && selected.get(key)!.amount !== item.amount) {
          throw new WorkerError("invalid_inventory");
        }
        selected.set(key, item);
      }
      const inventory = p.selected_assets ? { assets: [...selected.values()], failures: [], simulation: transport.simulation } :
        await collectInventory(transport, p.inventories!, signal);
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
        const sent = await transport.send(p.trade_url!, inventory.assets, signal, p.comment);
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
