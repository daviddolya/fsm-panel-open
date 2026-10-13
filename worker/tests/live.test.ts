import { expect, it } from "vitest";
import { EventEmitter } from "node:events";
import { LiveTransport, loadSdk } from "../src/live.ts";
import type { Sdk } from "../src/live.ts";

function fakeSdk() {
  let cookieCalls = 0, sendCalls = 0, loginCalls = 0;
  const user = Object.assign(new EventEmitter(), {
    logOn: () => { loginCalls++; queueMicrotask(() => {
      user.emit("webSession", "session", ["synthetic-cookie"]);
      user.emit("webSession", "session-again", ["synthetic-cookie"]);
    }); }, logOff: () => {},
  });
  const community = Object.assign(new EventEmitter(), {
    setCookies: () => {}, acceptConfirmationForObject: (_s: string, _id: string,
      done: (error?: unknown) => void) => done(),
  });
  const offer = { id: "12345", state: 2, isOurOffer: true, partner: { accountid: 123 },
    addMyItems: (items: object[]) => items.length,
    send: (done: (e: unknown, s: string) => void) => { sendCalls++; done(null, "sent"); done(null, "pending"); },
  };
  const manager = Object.assign(new EventEmitter(), {
    setCookies: (_c: string[], done: (error?: unknown) => void) => { cookieCalls++; done(); done(); },
    getInventoryContents: (_a: number, _c: string, _t: boolean, done: (e: unknown, x: []) => void) => done(null, []),
    createOffer: () => offer, getOffer: (_id: string, done: (e: unknown, value: typeof offer) => void) => done(null, offer),
    shutdown: () => {},
  });
  const sdk = { user: () => user, community: () => community,
    manager: () => manager, authCode: () => "SYNTHETIC" } as Sdk;
  return { sdk, user, manager, offer, counts: () => ({ cookieCalls, sendCalls, loginCalls }) };
}
const credentials = { account_name: "synthetic", password: "private-pass", shared_secret: "private-shared" };
it("loads the clean pinned SDK modules without constructing/authenticating a Steam client", () => {
  const sdk = loadSdk();
  expect(typeof sdk.user).toBe("function");
  expect(typeof sdk.authCode).toBe("function");
});
it("registers one webSession handler and settles duplicate callbacks once", async () => {
  const fake = fakeSdk(); const transport = new LiveTransport(fake.sdk);
  const signal = new AbortController().signal;
  await transport.connect(credentials, signal);
  expect(await transport.send("https://steamcommunity.com/tradeoffer/new/?partner=123", [], signal))
    .toEqual({ offer_id: "12345", status: "sent" });
  expect(fake.counts()).toEqual({ cookieCalls: 1, sendCalls: 1, loginCalls: 1 });
  transport.close();
});
it("disconnect errors contain fixed codes and terminate pending requests", async () => {
  const fake = fakeSdk(); const transport = new LiveTransport(fake.sdk);
  const signal = new AbortController().signal;
  await transport.connect(credentials, signal);
  fake.user.emit("disconnected", new Error("private-pass and cookie"));
  await expect(transport.inventory({ app_id: 730, context_id: "2" }, undefined, signal))
    .rejects.toMatchObject({ code: "transport_lost", message: "transport_lost" });
  transport.close();
});
it("reconciliation validates outgoing ownership and partner before trusting Steam state", async () => {
  const fake = fakeSdk(); const transport = new LiveTransport(fake.sdk);
  const signal = new AbortController().signal;
  await transport.connect(credentials, signal);
  expect(await transport.reconcile("12345", "999", signal)).toBe("unknown_outcome");
  fake.offer.isOurOffer = false;
  expect(await transport.reconcile("12345", "123", signal)).toBe("unknown_outcome");
  fake.offer.isOurOffer = true;
  for (const [state, expected] of [[2,"sent"], [9,"created_pending_confirmation"],
    [7,"failed"], [1,"unknown_outcome"]] as const) {
    fake.offer.state = state;
    expect(await transport.reconcile("12345", "123", signal)).toBe(expected);
  }
  transport.close();
});
