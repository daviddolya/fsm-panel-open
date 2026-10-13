import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import { WorkerError } from "./types.ts";
import type { Request, TradeResult, TradeState } from "./types.ts";

type Row = TradeResult & { fingerprint: string; partner_id: string; phase: string };
export class Journal {
  private db: DatabaseSync;
  readonly simulation: boolean;
  constructor(path: string, simulation: boolean) {
    this.simulation = simulation;
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;");
    const version = this.db.prepare("PRAGMA user_version").get()?.user_version;
    if (version !== 0 && version !== 1) throw new WorkerError("journal_error");
    this.db.exec(`CREATE TABLE IF NOT EXISTS operations (
      operation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
      partner_id TEXT NOT NULL, state TEXT NOT NULL, phase TEXT NOT NULL,
      offer_id TEXT, code TEXT, updated_at_ms INTEGER NOT NULL, simulation INTEGER NOT NULL
    ); PRAGMA user_version=1;`);
    if (this.db.prepare("PRAGMA quick_check").get()?.quick_check !== "ok") {
      throw new WorkerError("journal_error");
    }
    // Preparing has not crossed the effect boundary. Sending/confirming is already
    // UNKNOWN, committed before dispatch; no startup path ever dispatches an offer.
    this.db.prepare(`UPDATE operations SET phase='idle', code='recovered_after_restart',
      updated_at_ms=? WHERE phase != 'idle'`).run(Date.now());
  }
  get(id: string, accountId: string): Row {
    const row = this.db.prepare("SELECT * FROM operations WHERE operation_id=?").get(id);
    if (!row) throw new WorkerError("operation_not_found");
    if (row.account_id !== accountId || Boolean(row.simulation) !== this.simulation) {
      throw new WorkerError("operation_conflict");
    }
    return { ...row, simulation: Boolean(row.simulation) } as Row;
  }
  reserve(request: Request): { row: Row; created: boolean } {
    const p = request.params;
    const inventories = [...new Map(p.inventories!.map(s =>
      [`${s.app_id}/${s.context_id}`, s])).values()].sort((a, b) =>
      `${a.app_id}/${a.context_id}`.localeCompare(`${b.app_id}/${b.context_id}`));
    const fingerprint = createHash("sha256").update(JSON.stringify([
      p.account_id, p.credentials!.account_name.toLowerCase(), p.trade_url,
      inventories, p.allow_partial ?? true, this.simulation,
    ])).digest("hex");
    const result = this.db.prepare(`INSERT OR IGNORE INTO operations VALUES
      (?,?,?,?, 'not_sent','preparing',NULL,NULL,?,?)`).run(
      p.operation_id!, p.account_id!, fingerprint,
      new URL(p.trade_url!).searchParams.get("partner")!, Date.now(), Number(this.simulation),
    );
    const row = this.get(p.operation_id!, p.account_id!);
    if (row.fingerprint !== fingerprint) throw new WorkerError("operation_conflict");
    return { row, created: result.changes === 1 };
  }
  update(id: string, accountId: string, state: TradeState, code: string | null = null,
         offerId?: string, phase = "idle"): TradeResult {
    this.get(id, accountId);
    this.db.prepare(`UPDATE operations SET state=?, phase=?, code=?, updated_at_ms=?,
      offer_id=COALESCE(?, offer_id) WHERE operation_id=?`).run(
      state, phase, code, Date.now(), offerId ?? null, id);
    return this.public(this.get(id, accountId));
  }
  claim(id: string, accountId: string, expected: TradeState, phase: string): void {
    this.get(id, accountId);
    const result = this.db.prepare(`UPDATE operations SET state='unknown_outcome',
      phase=?, code=NULL, updated_at_ms=? WHERE operation_id=? AND state=?`).run(
      phase, Date.now(), id, expected);
    if (result.changes !== 1) throw new WorkerError("operation_conflict");
  }
  public(row: TradeResult): TradeResult {
    const { operation_id, account_id, state, offer_id, code, updated_at_ms, simulation } = row;
    return { operation_id, account_id, state, offer_id, code, updated_at_ms, simulation };
  }
  close(): void { this.db.close(); }
}
