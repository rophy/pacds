interface Session {
  id: string;
  service: string;
  createdAt: number;
  lastAccessedAt: number;
}

export class SessionStore {
  private sessions = new Map<string, Session>();
  private idleTimeoutMs: number;

  constructor(idleTimeoutMs = 15 * 60 * 1000) {
    this.idleTimeoutMs = idleTimeoutMs;
  }

  create(service: string): string {
    const id = `sess-${crypto.randomUUID()}`;
    this.sessions.set(id, {
      id,
      service,
      createdAt: Date.now(),
      lastAccessedAt: Date.now(),
    });
    return id;
  }

  touch(id: string): Session | undefined {
    const session = this.sessions.get(id);
    if (!session) return undefined;
    session.lastAccessedAt = Date.now();
    return session;
  }

  cleanup(): number {
    const now = Date.now();
    let removed = 0;
    for (const [id, session] of this.sessions) {
      if (now - session.lastAccessedAt > this.idleTimeoutMs) {
        this.sessions.delete(id);
        removed++;
      }
    }
    return removed;
  }
}
