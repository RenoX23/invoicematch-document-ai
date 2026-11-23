"use strict";

const uploadForm = document.getElementById("upload-form");
const decisionForm = document.getElementById("decision-form");
const mappingForm = document.getElementById("mapping-form");
const orderForm = document.getElementById("order-form");
const orderPicker = document.getElementById("order");
const modePicker = document.getElementById("mode");
const orderNumber = document.getElementById("order-number");
const orderItems = document.getElementById("order-lines");
const orderStatus = document.getElementById("order-status");
const mappingStatus = document.getElementById("mapping-status");
const uploadStatus = document.getElementById("upload-status");
const decisionStatus = document.getElementById("decision-status");
const reviewSection = document.getElementById("review");
const reconcileButton = uploadForm.querySelector("button");
const saveButton = decisionForm.querySelector("button");
const mappingButton = mappingForm.querySelector("button");
const verdictLabels = {
  match: "Match",
  mismatch: "Mismatch",
  needs_review: "Needs review",
};
let reviewId = null;
let catalog = { custom: [], samples: [], live_enabled: false };
let editingVersion = null;

async function request(url, options) {
  const response = await fetch(url, options);
  const data = response.status === 204 ? null : await response.json();
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

function jsonPut(url, body) {
  return request(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function updateModes() {
  const custom = Boolean(orderPicker.selectedOptions[0]?.dataset.version);
  modePicker.options[1].disabled = custom;
  modePicker.options[2].disabled = custom || !catalog.live_enabled;
  if (modePicker.selectedOptions[0]?.disabled) modePicker.value = "rules";
  document.getElementById("edit-order").disabled = !custom;
}

async function loadOrders(selected = orderPicker.value) {
  ({ data: catalog } = await request("/api/orders"));
  const samples = catalog.samples.map(({ order_id: id }) => new Option(`${id} · Sample`, id));
  const custom = catalog.custom.map(({ order, version }) => {
    const option = new Option(`${order.order_id} · ${order.supplier_id}`, order.order_id);
    option.dataset.version = version;
    return option;
  });
  orderPicker.replaceChildren(...samples, ...custom);
  if ([...orderPicker.options].some((option) => option.value === selected)) {
    orderPicker.value = selected;
  }
  updateModes();
}

function addOrderItem(item = {}) {
  const fragment = document.getElementById("order-line-template").content.cloneNode(true);
  for (const input of fragment.querySelectorAll("input")) {
    input.value = item[input.dataset.key] ?? "";
  }
  fragment.querySelector("button").addEventListener("click", (event) => {
    if (orderItems.children.length > 1) event.currentTarget.closest("fieldset").remove();
  });
  orderItems.append(fragment);
}

function startOrder(saved = null) {
  editingVersion = saved?.version ?? null;
  orderForm.reset();
  orderNumber.value = saved?.order.order_id ?? "";
  orderNumber.readOnly = saved !== null;
  document.getElementById("supplier").value = saved?.order.supplier_id ?? "";
  orderItems.replaceChildren();
  for (const item of saved?.order.lines ?? [{}]) addOrderItem(item);
}

function reviewBusy(busy) {
  reconcileButton.disabled = busy;
  saveButton.disabled = busy;
  mappingButton.disabled = busy;
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
  const mapping = review.mappings.at(-1)?.mapping;
  const hasMapping = mapping && (mapping.confirm_supplier || Object.keys(mapping.items).length);
  document.getElementById("review-verdict").textContent =
    `${verdictLabels[review.result.verdict]}${hasMapping ? " with reviewer mapping" : ""} · ${review.order.order_id} · ${review.mode}`;
  renderList("findings", review.result.findings.map(describeFinding), "No discrepancies found.");
  const order = review.order;
  renderList(
    "order-values",
    [
      `Supplier ${order.supplier_id} · ${order.currency}`,
      ...(review.order_version ? [`Saved PO version ${review.order_version}`] : []),
      ...order.lines.map((line) => `${line.item_id} · ${line.quantity} × ${line.unit_price}`),
    ],
    "No purchase-order lines.",
  );
  const extracted = review.extraction;
  const headers = [
    extracted.invoice_number, extracted.supplier_id, extracted.currency, extracted.total,
    ...Object.values(extracted.adjustments),
  ];
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
      return [event.decision, date, verdictLabels[event.verdict], event.note].filter(Boolean).join(" · ");
    }),
    "No decision recorded.",
  );
  showMappings(review);
}

function showMappings(review) {
  mappingForm.hidden = review.order_version === null;
  const prior = review.mappings.at(-1)?.mapping;
  const supplier = document.getElementById("confirm-supplier");
  supplier.disabled = !review.extraction.supplier_id;
  supplier.checked = prior?.confirm_supplier ?? false;
  document.getElementById("supplier-mapping-label").textContent =
    `Confirm ${review.extraction.supplier_id?.value ?? "the extracted supplier"} as ${review.order.supplier_id}`;
  const controls = review.extraction.lines.map(({ item_id: id }) => {
    const label = document.createElement("label");
    label.textContent = `Invoice item ${id} `;
    const field = document.createElement("select");
    field.dataset.item = id;
    field.add(new Option("Use extracted item ID", ""));
    for (const item of review.order.lines) {
      if (item.item_id !== id) field.add(new Option(item.item_id, item.item_id));
    }
    field.value = prior?.items[id] ?? "";
    label.append(field);
    const row = document.createElement("p");
    row.append(label);
    return row;
  });
  document.getElementById("item-mappings").replaceChildren(...controls);
  document.getElementById("mapping-history").hidden = review.mappings.length === 0;
  renderList("mapping-history", review.mappings.map(({ mapping, result, recorded_at }) => {
    const labels = Object.entries(mapping.items).map(([id, target]) => `${id} → ${target}`);
    if (mapping.confirm_supplier) labels.unshift("Supplier confirmed");
    return `${new Date(recorded_at).toLocaleString()} · ${labels.join(", ") || "Mappings cleared"} · ${verdictLabels[result.verdict]}`;
  }), "");
}

startOrder();
loadOrders().catch((error) => { orderStatus.textContent = error.message; });
orderPicker.addEventListener("change", updateModes);
document.getElementById("add-order-line").addEventListener("click", () => addOrderItem());
document.getElementById("new-order").addEventListener("click", () => startOrder());
document.getElementById("edit-order").addEventListener("click", () => {
  const saved = catalog.custom.find(({ order }) => order.order_id === orderPicker.value);
  if (saved) startOrder(saved);
});
orderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = orderForm.querySelector("[type=submit]");
  button.disabled = true;
  try {
    const lines = [...orderItems.children].map((row) => ({
      item_id: row.querySelector("[data-key=item_id]").value.trim(),
      quantity: Number(row.querySelector("[data-key=quantity]").value),
      unit_price: row.querySelector("[data-key=unit_price]").value.trim(),
    }));
    const { data } = await jsonPut("/api/orders", {
      order: {
        order_id: orderNumber.value.trim(),
        supplier_id: document.getElementById("supplier").value.trim(),
        currency: "INR",
        lines,
      },
      expected_version: editingVersion,
    });
    await loadOrders(data.order.order_id);
    startOrder(data);
    orderStatus.textContent = "Purchase order saved.";
  } catch (error) {
    orderStatus.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = document.getElementById("invoice").files[0];
  if (!file) return;
  const query = new URLSearchParams({ order_id: orderPicker.value, mode: modePicker.value });
  const version = orderPicker.selectedOptions[0]?.dataset.version;
  if (version) query.set("order_version", version);
  reviewBusy(true);
  uploadStatus.textContent = "Reconciling…";
  decisionStatus.textContent = "";
  mappingStatus.textContent = "";
  try {
    const { data, status } = await request(`/api/reviews?${query}`, {
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
    reviewBusy(false);
  }
});

decisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!reviewId) return;
  const decision = document.getElementById("decision").value;
  const note = document.getElementById("note").value.trim() || null;
  reviewBusy(true);
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
    reviewBusy(false);
  }
});

mappingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!reviewId) return;
  reviewBusy(true);
  try {
    const rows = [...document.querySelectorAll("#item-mappings select")];
    const items = Object.fromEntries(rows.filter((row) => row.value).map((row) => [row.dataset.item, row.value]));
    const { data } = await jsonPut(`/api/reviews/${encodeURIComponent(reviewId)}/mapping`, {
      items,
      confirm_supplier: document.getElementById("confirm-supplier").checked,
    });
    showReview(data);
    mappingStatus.textContent = "Mapping saved with the review.";
  } catch (error) {
    mappingStatus.textContent = error.message;
  } finally {
    reviewBusy(false);
  }
});

document.getElementById("delete-session").addEventListener("click", async () => {
  if (!window.confirm("Clear the purchase orders and reviews in this browser session?")) return;
  try {
    await request("/api/session", { method: "DELETE" });
    window.location.replace("/");
  } catch (error) {
    orderStatus.textContent = error.message;
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
