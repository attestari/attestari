// Live demo: use the Attestari TypeScript SDK against a running server.
//   (build first: npm run build, with the server on http://localhost:8000)
import { AttestariClient } from "./dist/index.js";

const attestari = new AttestariClient("http://localhost:8000");

// 1. Remember two things about a user, months apart.
await attestari.add("Hi, my name is Dana. I live in Delhi.", {
  subjectId: "u1", validFrom: "2019-01-01", sourceRef: "msg-1",
});
await attestari.add("I moved to Berlin.", {
  subjectId: "u1", validFrom: "2026-03-01", sourceRef: "msg-2",
});

// 2. Bi-temporal recall — same question, different "as of".
console.log("lives now       :", await attestari.answer("where does the user live", { subjectId: "u1" }));
console.log("lived in 2020   :", await attestari.answer("where did the user live", { subjectId: "u1", asOf: "2020-01-01" }));

// 3. Provenance — trace the current fact to its source.
const top = (await attestari.search("where does the user live", { subjectId: "u1" }))[0];
const prov = await attestari.getProvenance(top.fact.fact_id);
console.log("provenance      :", JSON.stringify(prov.snippet), "from", prov.source_ref);

// 4. Tamper-evident audit + provable deletion.
console.log("audit chain ok  :", (await attestari.verifyAudit()).ok);
const cert = await attestari.forget("u1");
console.log("forgot u1       :", cert.facts_deleted, "facts; certificate", cert.certificate_id.slice(0, 8));
console.log("recall after    :", await attestari.answer("where does the user live", { subjectId: "u1" }));
