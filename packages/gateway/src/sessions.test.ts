import { describe, it, expect, vi } from "vitest";
import { SessionStore } from "./sessions.js";

describe("SessionStore", () => {
  it("creates a session and returns a session id", () => {
    const store = new SessionStore();
    const id = store.create("checkout-service");
    expect(id).toMatch(/^sess-/);
  });

  it("touch returns the session and updates lastAccessedAt", () => {
    const store = new SessionStore();
    const id = store.create("checkout-service");
    const session = store.touch(id);
    expect(session).toBeDefined();
    expect(session!.service).toBe("checkout-service");
  });

  it("touch returns undefined for unknown session", () => {
    const store = new SessionStore();
    expect(store.touch("sess-nonexistent")).toBeUndefined();
  });

  it("cleanup removes expired sessions", () => {
    const store = new SessionStore(1000);
    const id = store.create("svc");

    vi.useFakeTimers();
    vi.advanceTimersByTime(2000);

    const removed = store.cleanup();
    expect(removed).toBe(1);
    expect(store.touch(id)).toBeUndefined();

    vi.useRealTimers();
  });

  it("cleanup keeps active sessions", () => {
    const store = new SessionStore(5000);
    const id = store.create("svc");

    const removed = store.cleanup();
    expect(removed).toBe(0);
    expect(store.touch(id)).toBeDefined();
  });
});
