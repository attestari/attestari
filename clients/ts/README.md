# @attestari/client

TypeScript client for the [Attestari](../../README.md) memory engine REST API.
Mirrors the Python `Memory` surface; works in Node 18+ and the browser.

> **Prerequisite:** this is a REST client — an Attestari server must be running for it
> to talk to. Start one with `pip install "attestari[server]" && uvicorn
> attestari.server:app` (defaults to `http://localhost:8000`), then point the client
> at that URL.

```ts
import { AttestariClient } from "@attestari/client";

const attestari = new AttestariClient("http://localhost:8000");   // your running server

await attestari.add("My name is Dana. I live in Delhi.", { subjectId: "user_42", validFrom: "2019-01-01" });
await attestari.add("I moved to Berlin.", { subjectId: "user_42", validFrom: "2026-03-01" });

await attestari.answer("where does the user live", { subjectId: "user_42" });               // "Berlin"
await attestari.answer("where did the user live", { subjectId: "user_42", asOf: "2020-01-01" }); // "Delhi"

const cert = await attestari.forget("user_42");   // provable deletion + certificate
const audit = await attestari.verifyAudit();       // { ok: true, ... }
```

```bash
npm install
npm run build       # emits dist/
npm run typecheck   # tsc --noEmit
```
