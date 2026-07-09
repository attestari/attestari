# @notari/client

TypeScript client for the [Notari](../../README.md) memory engine REST API.
Mirrors the Python `Memory` surface; works in Node 18+ and the browser.

> **Prerequisite:** this is a REST client — a Notari server must be running for it
> to talk to. Start one with `pip install "notari[server]" && uvicorn
> notari.server:app` (defaults to `http://localhost:8000`), then point the client
> at that URL.

```ts
import { NotariClient } from "@notari/client";

const notari = new NotariClient("http://localhost:8000");   // your running server

await notari.add("My name is Dana. I live in Delhi.", { subjectId: "user_42", validFrom: "2019-01-01" });
await notari.add("I moved to Berlin.", { subjectId: "user_42", validFrom: "2026-03-01" });

await notari.answer("where does the user live", { subjectId: "user_42" });               // "Berlin"
await notari.answer("where did the user live", { subjectId: "user_42", asOf: "2020-01-01" }); // "Delhi"

const cert = await notari.forget("user_42");   // provable deletion + certificate
const audit = await notari.verifyAudit();       // { ok: true, ... }
```

```bash
npm install
npm run build       # emits dist/
npm run typecheck   # tsc --noEmit
```
