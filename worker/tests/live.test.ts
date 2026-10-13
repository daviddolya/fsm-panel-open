import { expect, it, vi } from "vitest";
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
  return { sdk, user, manager, community, offer, counts: () => ({ cookieCalls, sendCalls, loginCalls }) };
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
it("refresh-token failure falls back to password once and never exposes token events", async () => {
  const fake=fakeSdk(); const calls:object[]=[];
  fake.user.logOn=(options?:object) => { calls.push(options!); queueMicrotask(()=>{
    if(calls.length===1) fake.user.emit("error",new Error("private-token"));
    else fake.user.emit("webSession","session",["synthetic-cookie"]);
  }); };
  const transport=new LiveTransport(fake.sdk);
  await transport.connect({...credentials,refresh_token:"private-token"},new AbortController().signal);
  fake.user.emit("loginKey","private-generated-token");
  expect(calls[0]).toEqual({refreshToken:"private-token"});
  expect(calls[1]).toMatchObject({accountName:"synthetic",password:"private-pass",rememberPassword:false});
  fake.user.emit("error",new Error("private-token"));
  expect(calls).toHaveLength(2);
  transport.close();
});
it("connection switches to WebSocket after ten seconds and stops twenty seconds later", async () => {
  vi.useFakeTimers();
  const fake=fakeSdk(); fake.user.logOn=()=>{};
  const switched:unknown[][]=[];
  Object.assign(fake.user,{setOption:(...args:unknown[])=>switched.push(args)});
  const transport=new LiveTransport(fake.sdk);
  try {
    const pending=transport.connect(credentials,new AbortController().signal).catch(error=>error);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(switched).toEqual([["protocol",2]]);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(await pending).toMatchObject({code:"deadline_exceeded"});
  } finally { transport.close(); vi.useRealTimers(); }
});
it("proxy configuration reaches both CM and community transports without network access", async () => {
  const fake=fakeSdk(); const options:object[]=[];
  fake.sdk.user=(config)=>{ options.push(config); return fake.user as ReturnType<Sdk["user"]>; };
  fake.sdk.proxyRequest=(proxy)=>({syntheticProxy:proxy});
  fake.sdk.community=(config)=>{ options.push(config!); return fake.community as ReturnType<Sdk["community"]>; };
  const transport=new LiveTransport(fake.sdk);
  await transport.connect({...credentials,proxy_url:"socks5://synthetic:private@127.0.0.1:1080"},new AbortController().signal);
  expect(options[0]).toMatchObject({socksProxy:"socks5://synthetic:private@127.0.0.1:1080"});
  expect(options[1]).toEqual({request:{syntheticProxy:"socks5://synthetic:private@127.0.0.1:1080"}});
  transport.close();
});
it("known confirmation acknowledgement workaround retries exactly once", async () => {
  const fake=fakeSdk(); let confirmations=0,acknowledgements=0;
  fake.community.acceptConfirmationForObject=(_secret,_id,done)=>{
    confirmations++; done(confirmations===1?new Error("Could not act on confirmation"):undefined);
  };
  Object.assign(fake.community,{getSessionID:()=>"private-session",
    httpRequestPost:(_url:string,_options:object,done:(error:unknown,response:object)=>void)=>{
      acknowledgements++; done(null,{statusCode:200});
    }});
  const transport=new LiveTransport(fake.sdk); const signal=new AbortController().signal;
  await transport.connect(credentials,signal);
  await transport.confirm("12345","private-identity",signal);
  expect({confirmations,acknowledgements}).toEqual({confirmations:2,acknowledgements:1});
  transport.close();
});
