"use strict";

const byId = (id) => document.getElementById(id);
const uploadForm = byId("upload-form");
const orderForm = byId("order-form");
const orderDialog = byId("order-dialog");
const orderPicker = byId("order");
const savedOrders = byId("saved-orders");
const orderItems = byId("order-lines");
const fileInput = byId("invoice");
const modePicker = byId("mode");
const decisionForm = byId("decision-form");
const mappingForm = byId("mapping-form");
const findings = byId("findings");
const sourcePicker = byId("source-picker");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const mobile = matchMedia("(max-width: 690px)");
const numberFormat = new Intl.NumberFormat("en-IN");
const verdictLabels = { match: "Match", mismatch: "Mismatch", needs_review: "Needs review" };
const decisionLabels = { accept: "Accept", reject: "Reject", hold: "Hold" };
const modeLabels = { rules: "Local rules", replay: "Sample replay", live: "Live AI" };
const verdictTones = { match: "good", mismatch: "bad", needs_review: "check" };
let catalog = { custom: [], live_enabled: false };
let ordersReady = false;
let editingVersion = null;
let reviewId = null;
let mappingRevision = 0;
let samplePath = false;
let busy = false;
let savingOrder = false;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function status(id, message = "", error = false) {
  const node = byId(id);
  node.textContent = message;
  node.dataset.error = String(error);
}

function showNotice(message, error = true) {
  byId("session-status").hidden = !message;
  status("session-status", message, error);
}

async function request(url, options) {
  const response = await fetch(url, options);
  const data = response.headers.get("content-type")?.includes("application/json") ? await response.json() : null;
  if (!response.ok) {
    throw new Error(data?.detail?.message || `Request failed (${response.status}). Try again.`);
  }
  return { data, status: response.status };
}

function writeJson(url, method, body) {
  return request(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  byId("theme-switch").setAttribute("aria-pressed", String(theme === "dark"));
}

try {
  applyTheme(localStorage.getItem("invoicematch-theme") === "dark" ? "dark" : "light");
} catch {
  applyTheme("light");
}
byId("theme-switch").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(theme);
  try {
    localStorage.setItem("invoicematch-theme", theme);
  } catch {
    // The theme still works for this page when storage is unavailable.
  }
});

function updateControls() {
  byId("upload-fields").disabled = busy;
  uploadForm.querySelector("[type=submit]").disabled = !ordersReady;
  byId("edit-order").disabled = !orderPicker.value;
  for (const id of ["custom-path", "sample-path", "new-review", "review-nav", "manage-orders", "delete-session"]) {
    byId(id).disabled = busy;
  }
  for (const form of [decisionForm, mappingForm]) {
    form.querySelector("[type=submit]").disabled = busy;
  }
}

function setBusy(next) {
  busy = next;
  updateControls();
}

function renderOrders(selected = orderPicker.value) {
  const options = catalog.custom.map(({ order, version }) => {
    const option = new Option(`${order.order_id} · ${order.supplier_id}`, order.order_id);
    option.dataset.version = version;
    return option;
  });
  const placeholder = new Option(options.length ? "Choose a saved order" : "No saved orders yet", "");
  placeholder.disabled = true;
  orderPicker.replaceChildren(placeholder, ...options);
  orderPicker.value = options.some((option) => option.value === selected) ? selected : "";
  modePicker.querySelector('[value="live"]').disabled = !catalog.live_enabled;
  if (!catalog.live_enabled && modePicker.value === "live") modePicker.value = "replay";
  byId("mode-hint").textContent = catalog.live_enabled
    ? "Replay uses a saved reading of the sample. Live AI reads the bundled PDF on demand."
    : "Replay uses a saved reading of the sample. Live AI is unavailable on this server.";
  updateControls();
}

async function loadOrders() {
  ({ data: catalog } = await request("/api/orders"));
  ordersReady = true;
  renderOrders();
}

function updateLineButtons() {
  for (const button of orderItems.querySelectorAll(".remove-line")) {
    button.disabled = orderItems.children.length === 1;
  }
  byId("add-order-line").disabled = orderItems.children.length >= 500;
}

function addOrderItem(item = {}) {
  const fragment = byId("order-line-template").content.cloneNode(true);
  for (const input of fragment.querySelectorAll("input")) {
    input.value = item[input.dataset.key] ?? "";
  }
  fragment.querySelector("button").addEventListener("click", (event) => {
    const row = event.currentTarget.closest("fieldset");
    const next = row.nextElementSibling || row.previousElementSibling;
    row.remove();
    updateLineButtons();
    next?.querySelector("input").focus();
  });
  orderItems.append(fragment);
}

function startOrder(saved = null) {
  editingVersion = saved?.version ?? null;
  orderForm.reset();
  byId("order-number").value = saved?.order.order_id ?? "";
  byId("order-number").readOnly = saved !== null;
  byId("supplier").value = saved?.order.supplier_id ?? "";
  orderItems.replaceChildren();
  for (const item of saved?.order.lines ?? [{}]) addOrderItem(item);
  updateLineButtons();
  status("order-status");
}

function openOrder(id = "") {
  savedOrders.replaceChildren(
    new Option("Create a new purchase order", ""),
    ...catalog.custom.map(({ order, version }) => new Option(`${order.order_id} · Version ${version}`, order.order_id)),
  );
  byId("catalog-label").hidden = !catalog.custom.length;
  savedOrders.value = id;
  startOrder(catalog.custom.find(({ order }) => order.order_id === id));
  orderDialog.showModal();
  byId(id ? "supplier" : "order-number").focus();
}

function setPath(useSample) {
  samplePath = useSample;
  byId("custom-inputs").hidden = useSample;
  byId("sample-inputs").hidden = !useSample;
  byId("custom-path").setAttribute("aria-pressed", String(!useSample));
  byId("sample-path").setAttribute("aria-pressed", String(useSample));
  status("upload-status");
}

function sampleName() {
  const chosen = uploadForm.querySelector('input[name="sample"]:checked').value;
  return chosen === "duplicate" ? "clean-match" : chosen;
}

function updateFile() {
  const file = fileInput.files[0];
  byId("file-name").textContent = file ? file.name : "Choose a PDF";
  byId("file-prompt").textContent = file ? "Click to replace this file" : "or drop it here";
  status("upload-status");
}

function scrollToNode(node) {
  node.scrollIntoView({ block: "nearest", behavior: reducedMotion.matches ? "instant" : "smooth" });
}

function changeScreen(showResult, moveFocus) {
  byId("setup").hidden = showResult;
  byId("review").hidden = !showResult;
  if (moveFocus) {
    byId(showResult ? "review-title" : "setup-title").focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
  }
}

function storeReviewUrl(id) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("review", id);
  else url.searchParams.delete("review");
  window.history.replaceState(null, "", url);
}

function formatMoney(value) {
  // Preserve the server's decimal precision, including totals beyond JS's safe integer range.
  const parts = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(String(value));
  if (!parts) return value;
  const [, sign, integer, fraction = ""] = parts;
  return `${sign}₹${numberFormat.format(BigInt(integer))}.${fraction.padEnd(2, "0")}`;
}

const fieldLabels = {
  invoice_number: "Invoice number", order_id: "Purchase order", supplier_id: "Supplier", currency: "Currency",
  item_id: "Item code", lines: "Invoice items", quantity: "Quantity", unit_price: "Unit price",
  line_total: "Line total", subtotal: "Subtotal", total: "Invoice total",
  tax: "Tax", cgst: "CGST", sgst: "SGST", igst: "IGST", freight: "Freight", discount: "Discount",
  extraction: "Live AI",
};
const findingLabels = {
  unverified_header: "Could not verify this field", unverified_line: "Could not verify this item",
  incomplete_table: "Could not read the full item table", unsupported_currency: "Only INR is supported",
  ambiguous_tax_components: "Tax amounts are unclear", overlapping_source_rows: "Source lines overlap",
  repeated_line_id: "Item code appears more than once", missing_order_line: "Missing from the invoice",
  unexpected_invoice_line: "Missing from the order", line_total_mismatch: "Does not equal quantity × unit price",
  invoice_subtotal_mismatch: "Does not equal the sum of invoice items",
  invoice_total_mismatch: "Does not equal the calculated invoice total",
  conflicting_order_selection: "This PDF was already compared with a different purchase order",
  possible_duplicate: "Another PDF has this supplier and invoice number",
  provider_timeout: "Live AI timed out",
  provider_connection_failed: "Could not reach the live AI provider",
  groq_http_429: "The live AI provider reached its request limit",
};
const moneyFields = new Set(["unit_price", "line_total", "subtotal", "total", "tax", "cgst", "sgst", "igst", "freight", "discount"]);
const fieldLabel = (field) => fieldLabels[field] || "Invoice check";
const sourceKey = ({ page, line, excerpt }) => JSON.stringify([page, line, excerpt]);
const sourceLocation = ({ page, line }) => `Page ${page} · Line ${line}`;

function displayValue(field, value) {
  if (value === null) return "—";
  return moneyFields.has(field) ? formatMoney(value) : value;
}

function badge(label, tone) {
  const node = element("span", label, "badge");
  node.dataset.tone = tone;
  return node;
}

function sourceCard(source, id) {
  const card = element("div", undefined, "source-card");
  card.id = id;
  card.append(
    element("p", sourceLocation(source), "source-location"),
    element("blockquote", source.excerpt, "source-excerpt"),
  );
  return card;
}

function selectSource(source, trigger) {
  const alreadyOpen = trigger?.getAttribute("aria-expanded") === "true";
  findings.querySelector('[aria-expanded="true"]')?.setAttribute("aria-expanded", "false");
  findings.querySelector(".evidence-inline")?.remove();
  if (alreadyOpen) return;
  sourcePicker.value = sourceKey(source);
  byId("active-source").replaceChildren(sourceCard(source, "excerpt"));
  if (!trigger) return;
  const inline = element("tr", undefined, "evidence-inline");
  const cell = element("td");
  cell.colSpan = 4;
  cell.append(sourceCard(source, "inline-excerpt"));
  inline.append(cell);
  trigger.closest("tr").after(inline);
  trigger.setAttribute("aria-expanded", "true");
  trigger.setAttribute("aria-controls", mobile.matches ? "inline-excerpt" : "active-source");
  scrollToNode(mobile.matches ? inline : byId("active-source"));
}

mobile.addEventListener("change", () => {
  const selected = findings.querySelector('[aria-expanded="true"]');
  selected?.setAttribute("aria-controls", mobile.matches ? "inline-excerpt" : "active-source");
});

function comparisonRow({ title, detail, expected, observed, result, tone, evidence }) {
  const row = element("tr");
  const heading = element("th");
  heading.scope = "row";
  if (evidence) {
    const button = element("button", title, "text-button table-link");
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    button.addEventListener("click", () => selectSource(evidence, button));
    heading.append(button);
  } else {
    heading.textContent = title;
  }
  if (detail) heading.append(element("span", detail, "row-detail"));
  const resultCell = element("td");
  resultCell.append(badge(result, tone));
  row.append(heading, element("td", expected), element("td", observed), resultCell);
  return row;
}

function findingRow(finding) {
  const differs = finding.code.endsWith("_mismatch")
    || ["missing_order_line", "unexpected_invoice_line"].includes(finding.code);
  return comparisonRow({
    title: [fieldLabel(finding.field), finding.item_id].filter(Boolean).join(" · "),
    detail: findingLabels[finding.code] ?? (differs ? "" : "Could not verify this check"),
    expected: displayValue(finding.field, finding.expected),
    observed: displayValue(finding.field, finding.observed),
    result: differs ? "Differs" : "Check",
    tone: differs ? "bad" : "check",
    evidence: finding.evidence,
  });
}

function showComparison(review, mapping) {
  const notes = review.result.findings;
  const orderItemsById = new Map(review.order.lines.map((line) => [line.item_id, line]));
  const flaggedItems = new Set(notes.map((finding) => finding.item_id));
  const rows = notes.map(findingRow);
  for (const line of review.extraction.lines) {
    if (flaggedItems.has(line.item_id)) continue;
    const orderLine = orderItemsById.get(mapping?.items[line.item_id] ?? line.item_id);
    const verified = review.result.verdict !== "needs_review" && orderLine;
    rows.push(comparisonRow({
      title: line.item_id,
      detail: orderLine && orderLine.item_id !== line.item_id ? `Order item ${orderLine.item_id}` : "Quantity × unit price",
      expected: orderLine ? `${orderLine.quantity} × ${formatMoney(orderLine.unit_price)}` : "—",
      observed: `${line.quantity} × ${formatMoney(line.unit_price)}`,
      result: verified ? "Matches" : "Unverified",
      tone: verified ? "good" : "quiet",
      evidence: line.evidence,
    }));
  }
  findings.replaceChildren(...rows);
  byId("order-values").replaceChildren(...review.order.lines.map((line) => element(
    "li", `${line.item_id} · ${line.quantity} × ${formatMoney(line.unit_price)}`,
  )));
}

function showEvidence(review) {
  const extracted = review.extraction;
  const entries = new Map();
  function add(evidence, label) {
    if (evidence && !entries.has(sourceKey(evidence))) entries.set(sourceKey(evidence), { evidence, label });
  }
  for (const finding of review.result.findings) add(finding.evidence, finding.item_id || fieldLabel(finding.field));
  for (const field of ["invoice_number", "supplier_id", "currency", "total"]) add(extracted[field]?.evidence, fieldLabel(field));
  for (const [field, claim] of Object.entries(extracted.adjustments)) add(claim.evidence, fieldLabel(field));
  for (const line of extracted.lines) add(line.evidence, line.item_id);

  const sources = [...entries.values()];
  sourcePicker.replaceChildren(...sources.map(({ evidence, label }) => new Option(
    `Page ${evidence.page} · ${label}`, sourceKey(evidence),
  )));
  sourcePicker.hidden = byId("source-label").hidden = sources.length < 2;
  sourcePicker.onchange = () => selectSource(entries.get(sourcePicker.value).evidence);
  if (sources.length) selectSource(sources[0].evidence);
  else byId("active-source").replaceChildren(element("p", "No invoice excerpt could be verified.", "hint"));
}

function showMappings(review, prior) {
  const panel = byId("mapping-panel");
  panel.hidden = review.order_version === null || (!review.extraction.supplier_id && !review.extraction.lines.length);
  byId("review-mapping").hidden = panel.hidden || !review.result.findings.some(
    ({ code }) => code === "supplier_mismatch" || code === "unexpected_invoice_line",
  );
  if (panel.hidden) return;
  const supplier = byId("confirm-supplier");
  supplier.disabled = !review.extraction.supplier_id;
  supplier.checked = prior?.confirm_supplier ?? false;
  byId("supplier-mapping-label").textContent = `Confirm ${review.extraction.supplier_id?.value ?? "the extracted supplier"} as ${review.order.supplier_id}`;
  const controls = review.extraction.lines.map(({ item_id: id }) => {
    const label = element("label", `Invoice item ${id}`, "field");
    const select = element("select");
    select.dataset.item = id;
    select.add(new Option("Keep invoice item code", ""));
    for (const item of review.order.lines) {
      if (item.item_id !== id) select.add(new Option(item.item_id, item.item_id));
    }
    select.value = prior?.items[id] ?? "";
    label.append(select);
    return label;
  });
  byId("item-mappings").replaceChildren(...controls);
  byId("mapping-history").replaceChildren(...review.mappings.map(({ mapping, result, recorded_at }) => {
    const labels = Object.entries(mapping.items).map(([id, target]) => `${id} → ${target}`);
    if (mapping.confirm_supplier) labels.unshift("Supplier confirmed");
    return element("li", `${new Date(recorded_at).toLocaleString()} · ${labels.join(", ") || "Names reset"} · ${verdictLabels[result.verdict]}`);
  }));
}

function showHistory(events) {
  const rows = events.map(({ decision, recorded_at, verdict, note }) => {
    const row = element("li");
    const time = element("time", new Date(recorded_at).toLocaleString());
    time.dateTime = recorded_at;
    const meta = element("small");
    meta.append(time);
    if (verdictLabels[verdict]) meta.append(` · ${verdictLabels[verdict]}`);
    row.append(element("strong", decisionLabels[decision]), meta);
    if (note) row.append(element("p", note, "event-note"));
    return row;
  });
  byId("history").replaceChildren(...(rows.length ? rows : [element("li", "No decision saved yet.")]));
}

function summarize(review) {
  const { verdict, findings: notes } = review.result;
  if (verdict === "match") return "The invoice matches the purchase order on the checks performed.";
  if (verdict === "needs_review") {
    if (notes.some(({ field }) => field === "extraction")) {
      return "Live AI could not read this sample. Start a new review and choose replay or local rules.";
    }
    if (notes.some(({ code }) => code === "possible_duplicate" || code === "conflicting_order_selection")) {
      return "This invoice may overlap with an earlier review. Check the flagged entries before deciding.";
    }
    return "Some invoice values could not be verified. Check the findings and the available evidence.";
  }
  const first = notes[0];
  if (notes.length === 1 && first.code === "unit_price_mismatch") {
    return `Item ${first.item_id} was billed at ${formatMoney(first.observed)} per unit. The order lists ${formatMoney(first.expected)}.`;
  }
  return `${notes.length} ${notes.length === 1 ? "difference needs" : "differences need"} your attention.`;
}

function showReview(review, { moveFocus = false, duplicate = false } = {}) {
  const changed = reviewId !== review.review_id;
  if (changed || moveFocus) {
    decisionForm.reset();
    byId("mapping-panel").open = false;
    byId("order-details").open = false;
    status("mapping-status");
    status("decision-status");
  }
  reviewId = review.review_id;
  mappingRevision = review.mappings.length;
  const order = review.order;
  const prior = review.mappings.at(-1)?.mapping;
  const verdict = review.result.verdict;
  byId("review-title").textContent = review.extraction.invoice_number?.value ?? "Invoice review";
  byId("review-context").textContent = `Invoice review · ${modeLabels[review.mode]}`;
  byId("order-context").textContent = `${order.order_id} · ${order.supplier_id}${review.order_version ? ` · Version ${review.order_version}` : ""}`;
  byId("verdict-band").dataset.verdict = verdict;
  byId("review-verdict").replaceWith(Object.assign(badge(verdictLabels[verdict], verdictTones[verdict]), { id: "review-verdict" }));
  byId("review-summary").textContent = summarize(review);
  if (moveFocus) byId("review-note").hidden = true;
  const reviewNote = duplicate ? "This invoice was already reviewed with these settings. Its existing history is shown below." : "";
  const mappingNote = prior && (prior.confirm_supplier || Object.keys(prior.items).length) ? "Includes names confirmed by a reviewer." : "";
  if (moveFocus) byId("review-note").dataset.duplicate = reviewNote;
  const note = [byId("review-note").dataset.duplicate, mappingNote].filter(Boolean).join(" ");
  byId("review-note").textContent = note;
  byId("review-note").hidden = !note;
  showComparison(review, prior);
  showEvidence(review);
  showMappings(review, prior);
  showHistory(review.decisions);
  changeScreen(true, moveFocus);
}

function newReview() {
  if (byId("review").hidden) {
    changeScreen(false, true);
    return;
  }
  reviewId = null;
  storeReviewUrl(null);
  status("upload-status");
  showNotice("");
  changeScreen(false, true);
}

byId("custom-path").addEventListener("click", () => setPath(false));
byId("sample-path").addEventListener("click", () => setPath(true));
byId("new-review").addEventListener("click", newReview);
byId("review-nav").addEventListener("click", newReview);
byId("manage-orders").addEventListener("click", () => openOrder(orderPicker.value));
byId("new-order").addEventListener("click", () => openOrder());
byId("edit-order").addEventListener("click", () => openOrder(orderPicker.value));
byId("close-order").addEventListener("click", () => orderDialog.close());
byId("open-help").addEventListener("click", () => byId("help-dialog").showModal());
byId("close-help").addEventListener("click", () => byId("help-dialog").close());
byId("review-mapping").addEventListener("click", () => {
  const panel = byId("mapping-panel");
  panel.open = true;
  panel.querySelector("summary").focus({ preventScroll: true });
  scrollToNode(panel);
});
byId("add-order-line").addEventListener("click", () => {
  addOrderItem();
  updateLineButtons();
  orderItems.lastElementChild.querySelector("input").focus();
});
orderDialog.addEventListener("cancel", (event) => {
  if (savingOrder) event.preventDefault();
});
orderPicker.addEventListener("change", updateControls);
savedOrders.addEventListener("change", () => {
  startOrder(catalog.custom.find(({ order }) => order.order_id === savedOrders.value));
});
fileInput.addEventListener("change", updateFile);
byId("sample-inputs").addEventListener("change", () => {
  byId("download-sample").href = `/samples/${sampleName()}.pdf`;
  status("upload-status");
});

const dropArea = byId("drop-area");
dropArea.addEventListener("dragover", (event) => {
  if (!event.dataTransfer.types.includes("Files")) return;
  event.preventDefault();
  if (!busy) dropArea.classList.add("is-dragging");
});
dropArea.addEventListener("dragleave", (event) => {
  if (!dropArea.contains(event.relatedTarget)) dropArea.classList.remove("is-dragging");
});
dropArea.addEventListener("drop", (event) => {
  event.preventDefault();
  dropArea.classList.remove("is-dragging");
  if (busy) return;
  if (event.dataTransfer.files.length !== 1) {
    status("upload-status", "Drop one PDF at a time.", true);
    return;
  }
  fileInput.files = event.dataTransfer.files;
  updateFile();
});

orderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  savingOrder = true;
  byId("order-fields").disabled = true;
  byId("close-order").disabled = true;
  savedOrders.disabled = true;
  status("order-status", "Saving purchase order…");
  try {
    const lines = [...orderItems.children].map((row) => ({
      item_id: row.querySelector("[data-key=item_id]").value.trim(),
      quantity: Number(row.querySelector("[data-key=quantity]").value),
      unit_price: row.querySelector("[data-key=unit_price]").value.trim(),
    }));
    const { data } = await writeJson("/api/orders", "PUT", {
      order: {
        order_id: byId("order-number").value.trim(),
        supplier_id: byId("supplier").value.trim(),
        currency: "INR",
        lines,
      },
      expected_version: editingVersion,
    });
    catalog.custom = [
      ...catalog.custom.filter(({ order }) => order.order_id !== data.order.order_id),
      data,
    ];
    ordersReady = true;
    renderOrders(data.order.order_id);
    orderDialog.close();
    if (byId("setup").hidden) showNotice("Purchase order saved. Start a new review to use it.", false);
    else {
      setPath(false);
      status("upload-status", "Purchase order saved and selected.");
      orderPicker.focus();
    }
  } catch (error) {
    status("order-status", error.message, true);
  } finally {
    savingOrder = false;
    byId("order-fields").disabled = false;
    byId("close-order").disabled = false;
    savedOrders.disabled = false;
  }
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !ordersReady) return;
  const file = fileInput.files[0];
  if (!samplePath && !file) {
    status("upload-status", "Choose an invoice PDF first.", true);
    fileInput.focus();
    return;
  }
  if (!samplePath && !orderPicker.value) {
    status("upload-status", "Save or choose a purchase order first.", true);
    (catalog.custom.length ? orderPicker : byId("new-order")).focus();
    return;
  }
  if (!samplePath && file.size > 2 * 1024 * 1024) {
    status("upload-status", "Choose a PDF no larger than 2 MiB.", true);
    fileInput.focus();
    return;
  }
  const query = new URLSearchParams({
    order_id: samplePath ? "PO-001" : orderPicker.value,
    mode: samplePath ? modePicker.value : "rules",
  });
  if (!samplePath) query.set("order_version", orderPicker.selectedOptions[0].dataset.version);
  const checkDuplicate = samplePath && uploadForm.querySelector('input[name="sample"]:checked').value === "duplicate";
  setBusy(true);
  showNotice("");
  status("upload-status", "Comparing invoice…");
  try {
    let body = file;
    if (samplePath) {
      const response = await fetch(`/samples/${sampleName()}.pdf`);
      if (!response.ok) throw new Error("The sample PDF could not be opened. Try again.");
      body = await response.blob();
    }
    const upload = () => request(`/api/reviews?${query}`, {
      method: "POST", headers: { "Content-Type": "application/pdf" }, body,
    });
    let result = await upload();
    if (checkDuplicate) result = await upload();
    showReview(result.data, { moveFocus: true, duplicate: result.status === 200 });
    storeReviewUrl(reviewId);
    status("upload-status");
  } catch (error) {
    status("upload-status", error.message, true);
  } finally {
    setBusy(false);
  }
});

decisionForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!reviewId || busy) return;
  setBusy(true);
  status("decision-status", "Saving decision…");
  try {
    const decision = byId("decision").value;
    const note = byId("note").value.trim() || null;
    const { data } = await writeJson(`/api/reviews/${encodeURIComponent(reviewId)}/decisions`, "POST", {
      decision, note, expected_mapping_revision: mappingRevision,
    });
    showHistory(data.decisions);
    decisionForm.reset();
    status("decision-status", "Decision saved.");
    byId("decision").focus();
  } catch (error) {
    status("decision-status", error.message, true);
  } finally {
    setBusy(false);
  }
});

mappingForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!reviewId || busy) return;
  setBusy(true);
  status("mapping-status", "Comparing again…");
  try {
    const rows = [...byId("item-mappings").querySelectorAll("select")];
    const items = Object.fromEntries(rows.filter((row) => row.value).map((row) => [row.dataset.item, row.value]));
    const { data } = await writeJson(`/api/reviews/${encodeURIComponent(reviewId)}/mapping`, "PUT", {
      items, confirm_supplier: byId("confirm-supplier").checked, expected_mapping_revision: mappingRevision,
    });
    showReview(data);
    status("mapping-status", `Names saved. Updated result: ${verdictLabels[data.result.verdict]}.`);
  } catch (error) {
    status("mapping-status", error.message, true);
  } finally {
    setBusy(false);
  }
});

byId("delete-session").addEventListener("click", async () => {
  if (!window.confirm("Clear the purchase orders and reviews in this browser session?")) return;
  setBusy(true);
  try {
    await request("/api/session", { method: "DELETE" });
    window.location.replace("/");
  } catch (error) {
    showNotice(error.message);
    setBusy(false);
  }
});

const savedId = new URLSearchParams(window.location.search).get("review");
if (savedId) {
  setBusy(true);
  request(`/api/reviews/${encodeURIComponent(savedId)}`)
    .then(({ data }) => showReview(data, { moveFocus: true }))
    .catch((error) => showNotice(error.message))
    .finally(() => setBusy(false));
}
loadOrders().catch((error) => {
  showNotice(`Could not load purchase orders. ${error.message}`);
  updateControls();
});
