// Attestari TypeScript SDK — a thin typed client over the REST API.
// Mirrors the Python `Memory` surface. Works in Node 18+ and the browser (uses
// the global `fetch`).

export interface Fact {
  fact_id: string;
  subject: string;
  predicate: string;
  object: string;
  valid_from: string | null;
  valid_to: string | null;
  alive: boolean;
  confidence: number;
  subject_id: string | null;
  source_episode_id: string;
}

export interface SearchResult {
  score: number;
  fact: Fact;
}

export interface Provenance {
  fact_id: string;
  source_episode_id: string;
  recorded_at: string | null;
  snippet: string | null;
  source_ref: string | null;
}

export interface DeletionCertificate {
  certificate_id: string;
  subject_id: string;
  requested_by: string;
  episodes_deleted: number;
  facts_deleted: number;
  manifest_hash: string;
  issued_at: string | null;
  /** HMAC-SHA256 under a KEK-derived key; null when the server runs without ATTESTARI_KEK. */
  signature: string | null;
  algorithm: string | null;
  /** True on a `forget(…, dryRun)` preview: nothing was destroyed and the certificate is left unsigned. */
  dry_run: boolean;
}

export interface AuditReport {
  ok: boolean;
  entries: number;
  head: string;
  broken_at: number | null;
}

export interface ConflictGroup {
  subject: string;
  predicate: string;
  resolution: string;
  values: Array<{ object: string; valid_from: string; valid_to: string | null; alive: boolean }>;
}

export interface AddOptions {
  subjectId?: string;
  agentId?: string;
  sessionId?: string;
  orgId?: string;
  validFrom?: string;
  sourceRef?: string;
}

export interface SearchOptions {
  subjectId?: string;
  asOf?: string;
  limit?: number;
}

export class AttestariClient {
  constructor(private readonly baseUrl: string = "http://localhost:8000") {
    this.baseUrl = baseUrl.replace(/\/$/, "");
  }

  private async request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body ? { "content-type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      throw new Error(`Attestari ${method} ${path} failed: ${res.status} ${await res.text()}`);
    }
    return (await res.json()) as T;
  }

  private static qs(params: Record<string, string | number | undefined>): string {
    const sp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== "") sp.set(k, String(v));
    }
    const s = sp.toString();
    return s ? `?${s}` : "";
  }

  async add(text: string, opts: AddOptions = {}): Promise<{ fact_ids: string[] }> {
    return this.request("POST", "/v1/add", {
      text,
      subject_id: opts.subjectId,
      agent_id: opts.agentId,
      session_id: opts.sessionId,
      org_id: opts.orgId,
      valid_from: opts.validFrom,
      source_ref: opts.sourceRef,
    });
  }

  async search(query: string, opts: SearchOptions = {}): Promise<SearchResult[]> {
    const path = `/v1/search${AttestariClient.qs({
      q: query,
      subject_id: opts.subjectId,
      as_of: opts.asOf,
      limit: opts.limit,
    })}`;
    const r = await this.request<{ results: SearchResult[] }>("GET", path);
    return r.results;
  }

  /** Convenience: the object of the top-ranked fact, or null. */
  async answer(query: string, opts: SearchOptions = {}): Promise<string | null> {
    const results = await this.search(query, opts);
    return results.length ? results[0].fact.object : null;
  }

  async timeline(subjectId?: string): Promise<Fact[]> {
    const r = await this.request<{ edges: Fact[] }>(
      "GET",
      `/v1/timeline${AttestariClient.qs({ subject_id: subjectId })}`,
    );
    return r.edges;
  }

  async getProvenance(factId: string): Promise<Provenance> {
    return this.request("GET", `/v1/provenance/${encodeURIComponent(factId)}`);
  }

  /** Right-to-be-forgotten. Pass `dryRun` to preview the certificate that would
   *  be issued (counts + manifest) without destroying anything — the preview is
   *  left unsigned (`signature === null`), so it never verifies as a real deletion. */
  async forget(
    subjectId: string,
    requestedBy = "ts-sdk",
    dryRun = false,
  ): Promise<DeletionCertificate> {
    return this.request(
      "POST",
      `/v1/forget/${encodeURIComponent(subjectId)}${AttestariClient.qs({
        requested_by: requestedBy,
        dry_run: dryRun ? "true" : undefined,
      })}`,
    );
  }

  async conflicts(subjectId?: string): Promise<ConflictGroup[]> {
    const r = await this.request<{ conflicts: ConflictGroup[] }>(
      "GET",
      `/v1/conflicts${AttestariClient.qs({ subject_id: subjectId })}`,
    );
    return r.conflicts;
  }

  /** Verify the tamper-evident audit chain. `deep` also re-checks stored event
   *  content against the chain's digests (catches silent in-place edits). */
  async verifyAudit(deep = false): Promise<AuditReport> {
    return this.request("GET", `/v1/audit/verify${AttestariClient.qs({ deep: deep ? "true" : undefined })}`);
  }
}
