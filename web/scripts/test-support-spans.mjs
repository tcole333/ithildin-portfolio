import assert from "node:assert/strict";
import { resolve } from "node:path";
import { createJiti } from "jiti";

const jiti = createJiti(import.meta.url);
const { annotateSupportSpans, computeSupportMetrics } = jiti(
  resolve("./src/lib/supportSpans.ts"),
);

function run(name, fn) {
  try {
    fn();
    process.stdout.write(`PASS ${name}\n`);
  } catch (error) {
    process.stderr.write(`FAIL ${name}\n`);
    throw error;
  }
}

run("sentence with inline citation is supported and uncited sentence is unsupported", () => {
  const entries = [
    { key: "efta:EFTA000001", label: "EFTA000001", number: 1, url: "https://jmail.world/thread/EFTA000001" },
  ];
  const html = '<p>Supported claim<sup class="citation"><a data-citation-number="1" data-citation-key="efta:EFTA000001">1</a></sup>. Unsupported claim.</p>';
  const result = annotateSupportSpans(html, entries);

  assert.equal(result.metrics.total_sentences, 2);
  assert.equal(result.metrics.supported_sentences, 1);
  assert.equal(result.metrics.unsupported_sentences, 1);
});

run("citation in adjacent sentence does not support prior sentence", () => {
  const entries = [
    { key: "efta:EFTA000002", label: "EFTA000002", number: 1, url: "https://jmail.world/thread/EFTA000002" },
  ];
  const html = '<p>First sentence lacks citation. Second sentence has one<sup class="citation"><a data-citation-number="1" data-citation-key="efta:EFTA000002">1</a></sup>.</p>';
  const result = annotateSupportSpans(html, entries);
  const first = result.supportMap.spans[0];
  const second = result.supportMap.spans[1];

  assert.equal(first.supported, false);
  assert.equal(second.supported, true);
});

run("finding citations expand to finding plus resolved source nodes", () => {
  const entries = [
    {
      key: "finding:42",
      label: "Finding #42",
      number: 3,
      url: "https://example.test/finding/42",
      sources: [
        { key: "EFTA01234567", label: "EFTA01234567", url: "https://jmail.world/thread/EFTA01234567" },
        { key: "SEC:0000909518-01-000297", label: "SEC:0000909518-01-000297", url: "https://sec.example/0000909518-01-000297" },
      ],
    },
  ];

  const html = '<p>Sentence tied to finding<sup class="citation"><a data-citation-number="3" data-citation-key="finding:42">3</a></sup>.</p>';
  const result = annotateSupportSpans(html, entries);
  const span = result.supportMap.spans[0];

  assert.ok(span.node_keys.includes("citation:finding:42"));
  assert.ok(span.node_keys.includes("source:EFTA01234567"));
  assert.ok(span.node_keys.includes("source:SEC:0000909518-01-000297"));
  assert.deepEqual(result.metrics.source_fanout, {
    "source:EFTA01234567": 1,
    "source:SEC:0000909518-01-000297": 1,
  });
});

run("unreferenced citation entries are reported as orphan citations", () => {
  const entries = [
    { key: "efta:EFTA000003", label: "EFTA000003", number: 2, url: "https://jmail.world/thread/EFTA000003" },
  ];
  const html = "<p>No citation key appears in this sentence.</p>";
  const result = annotateSupportSpans(html, entries);
  const recomputed = computeSupportMetrics(result.supportMap);

  assert.equal(result.metrics.orphan_citations_count, 1);
  assert.deepEqual(result.supportMap.orphan_citations, ["efta:EFTA000003"]);
  assert.equal(recomputed.orphan_citations_count, 1);
});

process.stdout.write("All support-span unit checks passed.\n");
