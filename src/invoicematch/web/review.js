"use strict";

const uploadForm = document.getElementById("upload-form");
const decisionForm = document.getElementById("decision-form");
const uploadStatus = document.getElementById("upload-status");
const decisionStatus = document.getElementById("decision-status");
const reviewSection = document.getElementById("review");
const reconcileButton = uploadForm.querySelector("button");
const saveButton = decisionForm.querySelector("button");
const verdictLabels = {
  match: "Match",
  mismatch: "Mismatch",
  needs_review: "Needs review",
};
let reviewId = null;

async function request(url, options) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.detail?.message || "The request could not be completed.");
  }
  return { data, status: response.status };
}

function renderList(id, items, emptyMessage) {
  const parent = document.getElementById(id);
  const labels = items.length ? items : [emptyMessage];
  const rows = labels.map((text) => {
    const item = document.createElement("li");
    item.textContent = text;
    return item;
  });
  parent.replaceChildren(...rows);
}

function cite(evidence) {
  return `Page ${evidence.page}, line ${evidence.line} · ${evidence.excerpt}`;
}

function describeFinding(finding) {
  const parts = [finding.code.replaceAll("_", " ")];
  if (finding.item_id) parts.push(finding.item_id);
  if (finding.expected !== null) parts.push(`Expected ${finding.expected}`);
  if (finding.observed !== null) parts.push(`Observed ${finding.observed}`);
  if (finding.evidence) parts.push(cite(finding.evidence));
  return parts.join(" · ");
}

function showReview(review) {
  reviewId = review.review_id;
  reviewSection.hidden = false;
  document.getElementById("review-verdict").textContent =
    `${verdictLabels[review.result.verdict]} · ${review.order.order_id}`;
  renderList("findings", review.result.findings.map(describeFinding), "No discrepancies found.");
  const order = review.order;
  renderList(
    "order-values",
    [
      `Supplier ${order.supplier_id} · ${order.currency}`,
      ...order.lines.map((line) => `${line.item_id} · ${line.quantity} × ${line.unit_price}`),
    ],
    "No purchase-order lines.",
  );
  const extracted = review.extraction;
  const headers = [extracted.invoice_number, extracted.supplier_id, extracted.currency, extracted.total];
  renderList(
    "evidence",
    [
      ...headers.filter(Boolean).map((claim) => cite(claim.evidence)),
      ...extracted.lines.map((line) => cite(line.evidence)),
    ],
    "No invoice evidence could be verified.",
  );
  renderList(
    "history",
    review.decisions.map((event) => {
      const date = new Date(event.recorded_at).toLocaleString();
      return [event.decision, date, event.note].filter(Boolean).join(" · ");
    }),
    "No decision recorded.",
  );
}

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.getElementById("invoice").files[0];
  if (!file) return;
  const order = document.getElementById("order").value;
  reconcileButton.disabled = true;
  saveButton.disabled = true;
  uploadStatus.textContent = "Reconciling…";
  decisionStatus.textContent = "";
  try {
    const { data, status } = await request(`/api/reviews?order_id=${encodeURIComponent(order)}`, {
      method: "POST",
      headers: { "Content-Type": "application/pdf" },
      body: file,
    });
    showReview(data);
    const url = new URL(window.location.href);
    url.searchParams.set("review", reviewId);
    window.history.replaceState(null, "", url);
    uploadStatus.textContent = status === 200 ? "Existing review loaded." : "Review created.";
  } catch (error) {
    uploadStatus.textContent = error.message;
  } finally {
    reconcileButton.disabled = false;
    saveButton.disabled = false;
  }
});

decisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!reviewId) return;
  const decision = document.getElementById("decision").value;
  const note = document.getElementById("note").value.trim() || null;
  reconcileButton.disabled = true;
  saveButton.disabled = true;
  try {
    const { data } = await request(`/api/reviews/${encodeURIComponent(reviewId)}/decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, note }),
    });
    showReview(data);
    document.getElementById("note").value = "";
    decisionStatus.textContent = "Decision saved.";
  } catch (error) {
    decisionStatus.textContent = error.message;
  } finally {
    reconcileButton.disabled = false;
    saveButton.disabled = false;
  }
});

const savedId = new URLSearchParams(window.location.search).get("review");
if (savedId) {
  reconcileButton.disabled = true;
  request(`/api/reviews/${encodeURIComponent(savedId)}`)
    .then(({ data }) => showReview(data))
    .catch((error) => { uploadStatus.textContent = error.message; })
    .finally(() => { reconcileButton.disabled = false; });
}
