import { createRequire } from "node:module";
import { setTimeout as delay } from "node:timers/promises";
import type { EventEmitter } from "node:events";
import { checkpoint, WorkerError } from "./types.ts";
import type { Asset, Credentials, InventorySpec, Transport } from "./types.ts";

type Callback<T> = (error: unknown, value: T) => void;
interface User extends EventEmitter {
  logOn(options: object): void;
  logOff(): void;
  setOption(name: string, value: unknown): void;
  requestFreeLicense(appIds: number[], callback: (error: unknown, packages: number[], apps: number[]) => void): void;
  gamesPlayed(appIds: number[], force?: boolean): void;
  setPersona(state: number): void;
}
interface Item { appid: number; contextid: string; assetid: string; amount: number }
interface Offer {
  id?: string; state?: number; isOurOffer?: boolean; partner?: { accountid: number };
  addMyItems(items: Item[]): number;
  send(callback: Callback<string>): void;
  setMessage(message: string): void;
}
interface Community extends EventEmitter {
  setCookies(cookies: string[]): void;
  acceptConfirmationForObject(secret: string, id: string, callback: (error?: unknown) => void): void;
  getSessionID(): string;
  httpRequestPost(url: string, options: object,
                  callback: (error: unknown, response?: { statusCode: number }) => void): void;
}
interface Manager extends EventEmitter {
  setCookies(cookies: string[], callback: (error?: unknown) => void): void;
  getInventoryContents(app: number, context: string, tradable: boolean,
                       callback: Callback<Item[]>): void;
  createOffer(url: string): Offer;
  getOffer(id: string, callback: Callback<Offer>): void;
  shutdown(): void;
}
export interface Sdk {
  user(options: object): User;
  community(options?: object): Community;
  manager(options: object): Manager;
  authCode(secret: string): string;
  proxyRequest?(proxy: string): unknown;
}
export function loadSdk(): Sdk {
  const require = createRequire(import.meta.url);
  const UserClass = require("steam-user") as new(options: object) => User;
  const CommunityClass = require("steamcommunity") as new(options?: object) => Community;
  const ManagerClass = require("steam-tradeoffer-manager") as new(options: object) => Manager;
  const totp = require("steam-totp") as { getAuthCode(secret: string): string };
  return {
    user: options => new UserClass(options), community: options => new CommunityClass(options),
    manager: options => new ManagerClass(options), authCode: secret => totp.getAuthCode(secret),
    proxyRequest: proxy => {
      const request = require("request") as { defaults(options: object): unknown };
      if (new URL(proxy).protocol.startsWith("socks")) {
        const { SocksProxyAgent } = require("socks-proxy-agent") as { SocksProxyAgent: new(proxy: string) => unknown };
        return request.defaults({ agent: new SocksProxyAgent(proxy), forever: true });
      }
      return request.defaults({ proxy, forever: true });
    },
  };
}
function failure(error: unknown, fallback: string): WorkerError {
  // Only numeric error metadata is examined. Never retain/log SDK message, URL, or body.
  if (typeof error === "object" && error !== null) {
    const e = error as { eresult?: unknown; statusCode?: unknown };
    if (e.eresult === 84 || e.statusCode === 429) return new WorkerError("rate_limited");
  }
  return new WorkerError(fallback);
}
export class LiveTransport implements Transport {
  readonly simulation = false;
  private sdk: Sdk;
  private user?: User;
  private community?: Community;
  private manager?: Manager;
  private dead = new AbortController();
  private timers: ReturnType<typeof setTimeout>[] = [];
  constructor(sdk = loadSdk()) { this.sdk = sdk; }
  private call<T>(signal: AbortSignal, fallback: string,
                  start: (done: Callback<T>) => void): Promise<T> {
    const combined = AbortSignal.any([signal, this.dead.signal]);
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error: unknown, value?: T) => {
        if (settled) return;
        settled = true;
        combined.removeEventListener("abort", abort);
        if (error) reject(error instanceof WorkerError ? error : failure(error, fallback));
        else resolve(value as T);
      };
      const abort = () => finish(combined.reason);
      combined.addEventListener("abort", abort, { once: true });
      if (combined.aborted) { abort(); return; }
      try { start(finish); } catch (error) { finish(failure(error, fallback)); }
    });
  }
  async connect(credentials: Credentials, signal: AbortSignal, machineName?: string): Promise<void> {
    checkpoint(signal);
    this.user = this.sdk.user({
      dataDirectory: null, autoRelogin: false, renewRefreshTokens: false, enablePicsCache: false,
      ...(credentials.proxy_url ? {
        [new URL(credentials.proxy_url).protocol.startsWith("socks") ? "socksProxy" : "httpProxy"]: credentials.proxy_url,
      } : {}),
    });
    this.community = this.sdk.community(credentials.proxy_url ? {
      request: this.sdk.proxyRequest!(credentials.proxy_url),
    } : {});
    // No steam integration, periodic polling, persistence, or automatic offer cancellation.
    this.manager = this.sdk.manager({
      community: this.community, language: "en", pollInterval: -1,
      dataDirectory: null, savePollData: false, useAccessToken: true,
    });
    const die = (code: string) => this.dead.abort(new WorkerError(code));
    let ready = false, retriedPassword = false;
    const passwordLogon = () => ({ accountName: credentials.account_name, password: credentials.password,
      twoFactorCode: this.sdk.authCode(credentials.shared_secret), rememberPassword: false,
      ...(machineName ? { machineName } : {}) });
    this.user.on("error", () => {
      if (!ready && credentials.refresh_token && !retriedPassword && !signal.aborted && !this.dead.signal.aborted) {
        retriedPassword = true;
        try { this.user!.logOn(passwordLogon()); } catch { die("auth_failed"); }
      } else die("auth_failed");
    });
    this.user.on("disconnected", () => die("transport_lost"));
    this.user.on("steamGuard", () => die("interactive_auth_required"));
    this.community.on("sessionExpired", () => die("transport_lost"));
    this.manager.on("sessionExpired", () => die("transport_lost"));
    this.manager.on("error", () => die("transport_lost"));
    const clearTimers = () => { for (const timer of this.timers) clearTimeout(timer); this.timers = []; };
    this.user.once("loggedOn", clearTimers);
    this.timers.push(setTimeout(() => {
      if (ready || signal.aborted || this.dead.signal.aborted) return;
      try { this.user!.setOption("protocol", 2); } catch { die("transport_lost"); return; }
      this.timers.push(setTimeout(() => die("deadline_exceeded"), 20_000));
    }, 10_000));
    await this.call<void>(signal, "auth_failed", done => {
      // once is registered before logOn; repeated webSession cannot re-enter send.
      this.user!.once("webSession", (_sessionId: string, cookies: string[]) => {
        if (signal.aborted || this.dead.signal.aborted) return;
        ready = true;
        clearTimers();
        try {
          this.community!.setCookies(cookies);
          this.manager!.setCookies(cookies, error => done(
            error ? failure(error, "cookies_failed") : null, undefined));
        } catch (error) { done(failure(error, "cookies_failed"), undefined); }
      });
      this.user!.logOn(credentials.refresh_token ? { refreshToken: credentials.refresh_token } : passwordLogon());
    });
  }
  async inventory(spec: InventorySpec, _cursor: string | undefined, signal: AbortSignal) {
    // Upstream community3.50.3 follows more_items/last_assetid until the full inventory
    // is available. A page error rejects the entire inventory, matching our atomic unit.
    const items = await this.call<Item[]>(signal, "inventory_failed", done =>
      this.manager!.getInventoryContents(spec.app_id, spec.context_id, true, done));
    return { assets: items.map(item => ({
      app_id: Number(item.appid), context_id: String(item.contextid),
      asset_id: String(item.assetid), amount: Number(item.amount),
    })) };
  }
  async send(url: string, assets: Asset[], signal: AbortSignal, comment = ""):
    Promise<{ offer_id: string; status: "sent" | "pending" }> {
    const offer = this.manager!.createOffer(url);
    if (comment) offer.setMessage(comment);
    const count = offer.addMyItems(assets.map(item => ({ appid: item.app_id,
      contextid: item.context_id, assetid: item.asset_id, amount: item.amount })));
    if (count !== assets.length) throw new WorkerError("send_unknown");
    const status = await this.call<string>(signal, "send_unknown", done => offer.send(done));
    if (!offer.id || (status !== "sent" && status !== "pending")) {
      throw new WorkerError("send_unknown");
    }
    return { offer_id: String(offer.id), status };
  }
  async confirm(id: string, secret: string, signal: AbortSignal): Promise<void> {
    await this.call<void>(signal, "confirmation_unknown", done => {
      this.community!.acceptConfirmationForObject(secret, id, error => {
        if (!(error instanceof Error) || error.message !== "Could not act on confirmation" ||
            signal.aborted || this.dead.signal.aborted) { done(error, undefined); return; }
        // Recovered embedded source workaround, bounded to this exact failure and one retry.
        this.community!.httpRequestPost("https://steamcommunity.com//trade/new/acknowledge", {
          headers: { referer: "https://steamcommunity.com" }, json: true,
          form: { sessionid: this.community!.getSessionID(), message: 1 },
          checkJsonError: false, checkHttpError: false,
        }, (ackError, response) => {
          if (ackError || response?.statusCode !== 200 || signal.aborted || this.dead.signal.aborted) {
            done(new WorkerError("confirmation_unknown"), undefined); return;
          }
          this.community!.acceptConfirmationForObject(secret, id, retryError => done(retryError, undefined));
        });
      });
    });
  }
  async reconcile(id: string, partnerId: string, signal: AbortSignal) {
    const offer = await this.call<Offer>(signal, "status_unavailable", done =>
      this.manager!.getOffer(id, done));
    if (!offer.isOurOffer || String(offer.id) !== id ||
        String(offer.partner?.accountid) !== partnerId) return "unknown_outcome" as const;
    // ETradeOfferState from the pinned manager resources/ETradeOfferState.js.
    if (offer.state === 9) return "created_pending_confirmation" as const;
    if (offer.state === 2 || offer.state === 3 || offer.state === 11) return "sent" as const;
    if ([5, 6, 7, 8, 10].includes(offer.state ?? 0)) return "failed" as const;
    return "unknown_outcome" as const;
  }
  close(): void {
    for (const timer of this.timers) clearTimeout(timer);
    this.timers = [];
    this.dead.abort(new WorkerError("transport_lost"));
    try { this.manager?.shutdown(); } catch { /* best effort */ }
    try { this.user?.logOff(); } catch { /* best effort */ }
    this.user?.removeAllListeners("webSession");
    this.user = undefined; this.community = undefined; this.manager = undefined;
  }
  async freeLicenses(appIds: number[], signal: AbortSignal): Promise<number[]> {
    const apps = await this.call<number[]>(signal, "transport_lost", done =>
      this.user!.requestFreeLicense(appIds, (error, _packages, apps) => done(error, apps)));
    if (!Array.isArray(apps) || apps.some(id => !Number.isSafeInteger(id) || !appIds.includes(id))) {
      throw new WorkerError("internal_error");
    }
    return [...new Set(apps)];
  }
  startActivity(appIds: number[]): void {
    checkpoint(this.dead.signal);
    this.user!.setPersona(1);
    this.user!.gamesPlayed(appIds, true);
  }
  stopActivity(): void { this.user?.gamesPlayed([]); }
  async activityDelay(ms: number, signal: AbortSignal): Promise<void> {
    const combined = AbortSignal.any([signal, this.dead.signal]);
    try { await delay(ms, undefined, { signal: combined }); }
    catch { checkpoint(combined); }
    checkpoint(combined);
  }
}
