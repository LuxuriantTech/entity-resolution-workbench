const API = {
  fixtures: "/api/v1/fixtures",
  session: "/api/v1/session",
  mapping: "/api/v1/session/mapping",
  run: "/api/v1/session/run",
  results: "/api/v1/session/results",
  export: "/api/v1/session/export.csv",
};

const CANONICAL_FIELDS = ["source_id", "name", "brand", "sku", "category", "price"];
const FIELD_LABELS = {
  source_id: "Record ID",
  name: "Product name",
  brand: "Brand",
  sku: "SKU",
  category: "Category",
  price: "Price",
};
const DECISION_LABELS = {
  MATCH: "Strong match",
  REVIEW: "Needs review",
  NO_MATCH: "Not a match",
};
const REASON_LABELS = {
  equal_top_score: "Another pair has the same top score",
  insufficient_bilateral_margin: "The lead over another candidate is too small",
  duplicate_fingerprint: "A duplicate record makes this pair ambiguous",
  homonym_name: "The same normalized name appears more than once",
  blocked_out_rival: "A strong rival sits outside the blocking keys",
  contradiction: "At least one field strongly contradicts the match",
  not_admitted_by_blocking: "No deterministic blocking key admitted this pair",
  below_match_threshold: "The score is below the automatic-match threshold",
  insufficient_evidence: "Too few comparable fields support a match",
  missing_support: "Neither name nor SKU provides required support",
  no_comparable_components: "The pair has no comparable score components",
};

const state = {
  sessionId: null,
  previews: null,
  mapping: null,
  counts: null,
  decision: "",
  query: "",
  page: 1,
  pageSize: 25,
  total: 0,
  currentPair: null,
};
let pageErrorReturnTarget = null;
let dialogErrorReturnTarget = null;
let comparisonReturnPairId = null;

const element = (id) => document.getElementById(id);

function make(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = options.text;
  if (options.attrs) {
    for (const [name, value] of Object.entries(options.attrs)) {
      if (value !== null && value !== undefined) node.setAttribute(name, String(value));
    }
  }
  return node;
}

function setBusy(active, title = "", detail = "") {
  element("busy-panel").hidden = !active;
  document.body.setAttribute("aria-busy", active ? "true" : "false");
  for (const id of ["input-panel", "mapping-panel", "results-panel"]) {
    element(id).inert = active;
  }
  if (active) {
    element("busy-title").textContent = title;
    element("busy-detail").textContent = detail;
  }
  if (!active) restoreComparisonFocus();
}

function restoreComparisonFocus() {
  if (!comparisonReturnPairId || element("pair-dialog").open) return;
  const results = element("results-panel");
  if (!state.sessionId || results.hidden || results.inert) return;
  const opener = [...results.querySelectorAll(".pair-open")]
    .find((button) => button.dataset.pairId === comparisonReturnPairId);
  comparisonReturnPairId = null;
  (opener || element("results-title")).focus();
}

function keepComparisonFocus(event) {
  if (event.key !== "Tab") return;
  const dialog = element("pair-dialog");
  const controls = [...dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]")]
    .filter((node) => node.tabIndex >= 0 && !node.matches(":disabled")
      && !node.closest("[inert]") && node.getClientRects().length
      && getComputedStyle(node).visibility !== "hidden");
  const first = controls[0];
  const last = controls.at(-1);
  const active = document.activeElement;
  if (!first || !dialog.contains(active) || active === dialog
    || (event.shiftKey ? active === first : active === last)) {
    event.preventDefault();
    (event.shiftKey ? last : first)?.focus();
  }
}

function setStep(step) {
  for (const item of document.querySelectorAll(".journey li")) {
    const number = Number(item.dataset.step);
    if (number === step) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    if (number < step) item.dataset.complete = "true";
    else delete item.dataset.complete;
  }
}

function announce(message) {
  const node = element("status-announcer");
  node.textContent = "";
  window.setTimeout(() => { node.textContent = message; }, 10);
}

function errorMessage(error) {
  return error?.message || "The local workbench could not complete that request. Check the inputs and try again.";
}

function showError(error, returnTarget = null) {
  const panel = element("error-panel");
  pageErrorReturnTarget = returnTarget;
  element("error-message").textContent = errorMessage(error);
  panel.hidden = false;
  panel.focus();
  announce(element("error-message").textContent);
}

function showDialogError(error, returnTarget) {
  const panel = element("dialog-error-panel");
  dialogErrorReturnTarget = returnTarget;
  element("dialog-error-message").textContent = errorMessage(error);
  panel.hidden = false;
  panel.focus();
  announce(element("dialog-error-message").textContent);
}

function clearError() {
  element("error-panel").hidden = true;
  element("dialog-error-panel").hidden = true;
  pageErrorReturnTarget = null;
  dialogErrorReturnTarget = null;
}

function dismissError(panelId, target) {
  element(panelId).hidden = true;
  if (target?.isConnected) target.focus();
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (["POST", "PUT", "DELETE"].includes(options.method)) {
    headers.set("X-ERW-Request", "local-workbench-v1");
  }
  if (state.sessionId) headers.set("X-ERW-Session", state.sessionId);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    const detail = payload?.error || {};
    const failure = new Error(detail.message || `The local API returned ${response.status}.`);
    failure.code = detail.code || "HTTP_ERROR";
    failure.recoverable = detail.recoverable !== false;
    throw failure;
  }
  if (response.status === 204) return null;
  return response.json();
}

function sourceKind() {
  return document.querySelector('input[name="source-kind"]:checked')?.value || "fixture";
}

function updateSourceFields() {
  const uploading = sourceKind() === "upload";
  element("upload-fields").hidden = !uploading;
  element("synthetic-confirmation").hidden = !uploading;
  element("left-file").required = uploading;
  element("right-file").required = uploading;
  element("synthetic-only").required = uploading;
  element("prepare-button").textContent = uploading ? "Validate files" : "Prepare catalogues";
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const value = String(reader.result || "");
      const comma = value.indexOf(",");
      if (comma < 0) reject(new Error("The browser could not read this CSV file."));
      else resolve(value.slice(comma + 1));
    });
    reader.addEventListener("error", () => reject(new Error("The browser could not read this CSV file.")));
    reader.readAsDataURL(file);
  });
}

async function sessionRequest() {
  if (sourceKind() === "fixture") {
    return { fixture_id: "catalogue-desk-v1", synthetic_only: true };
  }
  const left = element("left-file").files[0];
  const right = element("right-file").files[0];
  if (!left || !right) throw new Error("Choose one CSV file for each catalogue.");
  if (!element("synthetic-only").checked) {
    throw new Error("Confirm that both files contain synthetic product catalogue data only.");
  }
  if (left.size > 1_000_000 || right.size > 1_000_000) {
    throw new Error("Each CSV file must be no larger than 1 MB.");
  }
  const [leftContent, rightContent] = await Promise.all([fileToBase64(left), fileToBase64(right)]);
  return {
    synthetic_only: true,
    uploads: {
      left: { filename: left.name, content_base64: leftContent },
      right: { filename: right.name, content_base64: rightContent },
    },
  };
}

function appendPreview(side, preview) {
  const card = make("article", { className: "preview-card" });
  card.append(
    make("p", { className: "eyebrow", text: side === "left" ? "Catalogue A" : "Catalogue B" }),
    make("h3", { text: preview.display_name }),
    make("p", { text: "Validated locally and ready to map." }),
  );
  const facts = make("ul", { className: "preview-facts" });
  facts.append(
    make("li", { text: `${preview.row_count} records` }),
    make("li", { text: `${preview.byte_count} bytes` }),
    make("li", { text: `${preview.headers.length} columns` }),
  );
  card.append(facts);
  element("catalogue-previews").append(card);
}

function mappingSelect(side, field, headers, suggestion) {
  const select = make("select", {
    attrs: {
      id: `mapping-${side}-${field}`,
      name: `${side}-${field}`,
      "data-side": side,
      "data-field": field,
      "aria-label": `${side === "left" ? "Catalogue A" : "Catalogue B"}: ${FIELD_LABELS[field]}`,
    },
  });
  select.append(make("option", { text: "Not mapped", attrs: { value: "" } }));
  for (const header of headers) {
    const option = make("option", { text: header, attrs: { value: header } });
    if (suggestion === header) option.selected = true;
    select.append(option);
  }
  return select;
}

function appendMapping(side, preview, suggestions) {
  const card = make("section", { className: "mapping-card" });
  card.append(make("h3", { text: side === "left" ? "Catalogue A columns" : "Catalogue B columns" }));
  const catalogueName = side === "left" ? "Catalogue A" : "Catalogue B";
  const wrap = make("div", {
    className: "mapping-table-wrap",
    attrs: { tabindex: "0", role: "region", "aria-label": `${catalogueName} column mapping` },
  });
  const table = make("table", { className: "mapping-table" });
  table.append(make("caption", { text: `Map ${preview.display_name} to the matcher schema.` }));
  const head = make("thead");
  const headRow = make("tr");
  headRow.append(
    make("th", { text: "Matcher field", attrs: { scope: "col" } }),
    make("th", { text: "Source column", attrs: { scope: "col" } }),
  );
  head.append(headRow);
  const body = make("tbody");
  for (const field of CANONICAL_FIELDS) {
    const row = make("tr");
    const label = make("label", { text: FIELD_LABELS[field], attrs: { for: `mapping-${side}-${field}` } });
    if (field === "source_id") label.append(" ", make("span", { className: "required", text: "Required" }));
    const heading = make("th", { attrs: { scope: "row" } });
    heading.append(label);
    const value = make("td");
    value.append(mappingSelect(side, field, preview.headers, suggestions?.[field] || ""));
    row.append(heading, value);
    body.append(row);
  }
  table.append(head, body);
  wrap.append(table);
  card.append(wrap);
  element("mapping-tables").append(card);
}

function renderMapping(payload) {
  state.previews = payload.previews;
  state.mapping = payload.suggested_mapping;
  element("catalogue-previews").replaceChildren();
  element("mapping-tables").replaceChildren();
  for (const side of ["left", "right"]) {
    appendPreview(side, payload.previews[side]);
    appendMapping(side, payload.previews[side], payload.suggested_mapping?.[side]);
  }
  element("input-panel").hidden = true;
  element("mapping-panel").hidden = false;
  element("results-panel").hidden = true;
  setStep(2);
  element("mapping-title").focus();
}

function collectMapping() {
  const mapping = { left: {}, right: {} };
  for (const side of ["left", "right"]) {
    for (const field of CANONICAL_FIELDS) {
      mapping[side][field] = element(`mapping-${side}-${field}`).value || null;
    }
  }
  return mapping;
}

function pairTitle(pair) {
  const left = pair.left?.raw || {};
  const right = pair.right?.raw || {};
  return `${left.name || pair.left_id} compared with ${right.name || pair.right_id}`;
}

function totalDisplay(pair) {
  return pair.scores?.total?.display ?? "0.000000";
}

function normalizedDisplay(record, field) {
  const value = record?.normalized?.[field];
  return value === null || value === "" || value === undefined ? "Missing" : value;
}

function pairSnapshot(label, record) {
  const snapshot = make("span", { className: "pair-record-snapshot" });
  snapshot.append(
    make("span", { className: "snapshot-label", text: label }),
    make("strong", { text: record?.raw?.name || record?.raw?.source_id || "Unnamed record" }),
  );
  for (const [field, fieldLabel] of [
    ["name", "Normalized name"],
    ["brand", "Normalized brand"],
    ["sku", "Normalized SKU"],
  ]) {
    const fact = make("span", { className: "snapshot-fact" });
    fact.append(
      make("span", { className: "snapshot-fact-label", text: fieldLabel }),
      make("span", { text: normalizedDisplay(record, field) }),
    );
    snapshot.append(fact);
  }
  return snapshot;
}

function visibleDecisionReasons(pair) {
  const reasons = [...(pair.failed_conditions || []), ...(pair.contradictions || [])];
  if (reasons.length) return [...new Set(reasons)];
  if (pair.decision === "MATCH") return ["automatic_match_rules_met"];
  return ["no_recorded_reason"];
}

function reasonLabel(value) {
  if (value === "automatic_match_rules_met") {
    return "Automatic-match rules were met with no recorded contradiction";
  }
  if (value === "no_recorded_reason") return "No additional decision reason was recorded";
  return REASON_LABELS[value] || value.replaceAll("_", " ");
}

function decisionReasonTitle(decision) {
  if (decision === "MATCH") return "Why this is a strong match";
  if (decision === "REVIEW") return "Why this needs review";
  return "Why this is not a match";
}

function pairEvidenceSummary(pair) {
  const body = make("span", { className: "pair-evidence-summary" });
  const records = make("span", {
    className: "pair-record-grid",
    attrs: { "aria-label": "Catalogue record comparison" },
  });
  records.append(pairSnapshot("Catalogue A", pair.left), pairSnapshot("Catalogue B", pair.right));

  const components = make("span", {
    className: "pair-components",
    attrs: { "aria-label": "Decomposed similarity evidence" },
  });
  for (const [field, label] of [
    ["name", "Product name similarity"],
    ["brand", "Brand similarity"],
    ["sku", "SKU similarity"],
    ["price", "Price similarity"],
    ["total", "Overall similarity"],
  ]) {
    const component = make("span", { className: "pair-component" });
    component.append(
      make("span", { text: label }),
      make("strong", { text: pair.scores?.[field]?.display ?? "Not compared" }),
    );
    components.append(component);
  }

  const reasons = make("span", { className: "pair-reasons" });
  reasons.append(make("strong", { text: decisionReasonTitle(pair.decision) }));
  const reasonList = make("span", { className: "pair-reason-list" });
  for (const reason of visibleDecisionReasons(pair)) {
    reasonList.append(make("span", { className: "pair-reason", text: reasonLabel(reason) }));
  }
  reasons.append(reasonList);
  body.append(records, components, reasons);
  return body;
}

function appendDecisionCounts(counts) {
  const container = element("decision-counts");
  container.replaceChildren();
  for (const decision of ["MATCH", "REVIEW", "NO_MATCH"]) {
    const button = make("button", {
      className: "decision-count",
      attrs: { type: "button", "data-decision": decision },
    });
    button.append(
      make("strong", { text: DECISION_LABELS[decision] }),
      make("span", { text: String(counts?.[decision] || 0) }),
    );
    button.addEventListener("click", () => {
      element("decision-filter").value = decision;
      state.decision = decision;
      state.page = 1;
      loadResults();
    });
    container.append(button);
  }
}

function appendPair(pair) {
  const item = make("li");
  const titleId = `pair-title-${pair.pair_id}`;
  const card = make("article", {
    className: "pair-card",
    attrs: {
      "data-pair-id": pair.pair_id,
      "data-decision": pair.decision,
      "aria-labelledby": titleId,
    },
  });
  card.append(make("span", {
    className: "decision-label",
    text: DECISION_LABELS[pair.decision] || pair.decision,
    attrs: { "data-decision": pair.decision },
  }));
  const summary = make("span", { className: "pair-summary" });
  summary.append(
    make("h3", { text: pairTitle(pair), attrs: { id: titleId } }),
    make("p", { text: `${pair.left_id} · ${pair.right_id}${pair.human_review && pair.human_review !== "UNREVIEWED" ? ` · Human review: ${pair.human_review.replaceAll("_", " ").toLowerCase()}` : ""}` }),
  );
  const score = make("span", { className: "score" });
  score.append(make("strong", { text: totalDisplay(pair) }), make("span", { text: "Overall similarity" }));
  const openButton = make("button", {
    className: "button secondary pair-open",
    text: "Open full comparison and review",
    attrs: { type: "button", "data-pair-id": pair.pair_id },
  });
  openButton.addEventListener("click", () => openPair(pair.pair_id));
  card.append(summary, score, pairEvidenceSummary(pair), openButton);
  item.append(card);
  element("pair-list").append(item);
}

function updatePagination(payload) {
  const totalPages = Math.max(1, Math.ceil(payload.total / payload.page_size));
  const pagination = element("pagination");
  pagination.hidden = payload.total <= payload.page_size;
  element("previous-page").disabled = payload.page <= 1;
  element("next-page").disabled = payload.page >= totalPages;
  element("page-status").textContent = `Page ${payload.page} of ${totalPages}`;
}

function renderResults(payload) {
  state.total = payload.total;
  state.page = payload.page;
  state.pageSize = payload.page_size;
  if (payload.counts) {
    state.counts = payload.counts;
    appendDecisionCounts(payload.counts);
  }
  element("pair-list").replaceChildren();
  for (const pair of payload.pairs) appendPair(pair);
  element("empty-results").hidden = payload.pairs.length !== 0;
  element("filter-summary").textContent = payload.total === 1
    ? "1 pair in this view."
    : `${payload.total} pairs in this view.`;
  updatePagination(payload);
  element("input-panel").hidden = true;
  element("mapping-panel").hidden = true;
  element("results-panel").hidden = false;
  setStep(4);
}

async function loadResults({ focus = false } = {}) {
  clearError();
  setBusy(true, "Filtering decisions", "Keeping every matching pair reachable.");
  try {
    const parameters = new URLSearchParams({
      page: String(state.page),
      page_size: String(state.pageSize),
    });
    if (state.decision) parameters.set("decision", state.decision);
    if (state.query) parameters.set("query", state.query);
    const payload = await api(`${API.results}?${parameters}`);
    renderResults(payload);
    if (focus) element("results-title").focus();
    announce(`${payload.total} matching pairs shown.`);
  } catch (error) {
    showError(error, element("results-title"));
  } finally {
    setBusy(false);
  }
}

function listOrNone(values) {
  const list = make("ul", { className: "tag-list" });
  const present = values && values.length ? values : ["None"];
  for (const value of present) list.append(make("li", { text: REASON_LABELS[value] || value.replaceAll("_", " ") }));
  return list;
}

function recordSide(label, record) {
  const section = make("section", { className: "record-side" });
  section.append(make("p", { className: "eyebrow", text: label }), make("h3", { text: record.raw.name || record.raw.source_id }));
  const list = make("dl");
  for (const field of CANONICAL_FIELDS) {
    list.append(make("dt", { text: FIELD_LABELS[field] }));
    const value = make("dd", { text: record.raw[field] || "Missing" });
    const normalized = record.normalized?.[field];
    value.append(make("span", {
      className: "normalized-value",
      text: `Normalized: ${normalized === null || normalized === "" || normalized === undefined ? "missing" : normalized}`,
    }));
    list.append(value);
  }
  section.append(list);
  return section;
}

function scoreTable(pair) {
  const wrap = make("div", {
    className: "evidence-table-wrap",
    attrs: { tabindex: "0", role: "region", "aria-label": "Similarity evidence table" },
  });
  const table = make("table", { className: "evidence-table" });
  table.append(make("caption", { text: "Decomposed similarity evidence. Scores are not probabilities." }));
  const head = make("thead");
  const row = make("tr");
  for (const heading of ["Component", "Display", "Exact fraction"]) {
    row.append(make("th", { text: heading, attrs: { scope: "col" } }));
  }
  head.append(row);
  const body = make("tbody");
  for (const field of ["name", "brand", "sku", "price", "total"]) {
    const score = pair.scores?.[field];
    const scoreRow = make("tr");
    scoreRow.append(
      make("th", { text: field === "total" ? "Total" : FIELD_LABELS[field], attrs: { scope: "row" } }),
      make("td", { text: score?.display ?? "Missing" }),
      make("td", { text: score?.fraction ?? "Not scored" }),
    );
    body.append(scoreRow);
  }
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function explanationCard(title, values) {
  const card = make("section", { className: "explanation-card" });
  card.append(make("h3", { text: title }), listOrNone(values));
  return card;
}

function reviewForm(pair) {
  const form = make("form", { className: "review-form", attrs: { id: "review-form" } });
  const fieldset = make("fieldset");
  fieldset.append(make("legend", { text: "Session-local human review" }));
  for (const [value, label] of [
    ["UNREVIEWED", "Leave unreviewed"],
    ["SAME_ENTITY", "Same product"],
    ["DIFFERENT_ENTITY", "Different products"],
  ]) {
    const choice = make("label");
    const input = make("input", { attrs: { type: "radio", name: "review", value } });
    if ((pair.human_review || "UNREVIEWED") === value) input.checked = true;
    choice.append(input, label);
    fieldset.append(choice);
  }
  fieldset.append(make("button", { className: "button primary", text: "Save review", attrs: { type: "submit" } }));
  form.append(fieldset, make("p", { text: "This annotation does not change the engine decision and disappears on reset." }));
  form.addEventListener("submit", saveReview);
  return form;
}

function renderPairDetail(pair) {
  state.currentPair = pair;
  element("dialog-decision").textContent = DECISION_LABELS[pair.decision] || pair.decision;
  element("pair-dialog-title").textContent = pairTitle(pair);
  const detail = element("pair-detail");
  detail.replaceChildren();
  const comparison = make("div", { className: "record-comparison" });
  comparison.append(recordSide("Catalogue A", pair.left), recordSide("Catalogue B", pair.right));
  detail.append(comparison, scoreTable(pair));
  const explanations = make("div", { className: "explanation-grid" });
  explanations.append(
    explanationCard("Why this pair was considered", pair.block_reasons),
    explanationCard("Automatic-match conditions not met", pair.failed_conditions),
    explanationCard("Contradictions", pair.contradictions),
    explanationCard("Missing score components", pair.missing_components),
  );
  detail.append(explanations);
  const margins = make("p", {
    text: `Ranks: ${pair.ranks.left} from catalogue A and ${pair.ranks.right} from catalogue B. Margins: ${pair.margins.left} and ${pair.margins.right}.`,
  });
  detail.append(margins);
  if (pair.decision === "REVIEW") detail.append(reviewForm(pair));
}

async function openPair(pairId) {
  clearError();
  setBusy(true, "Opening the comparison", "Loading the complete engine evidence.");
  try {
    const pair = await api(`/api/v1/session/pairs/${encodeURIComponent(pairId)}`);
    renderPairDetail(pair);
    comparisonReturnPairId = pairId;
    element("pair-dialog").showModal();
  } catch (error) {
    showError(error, element("results-title"));
  } finally {
    setBusy(false);
  }
}

async function saveReview(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const review = new FormData(form).get("review");
  const saveButton = form.querySelector('button[type="submit"]');
  setBusy(true, "Saving the review", "Keeping it separate from the engine decision.");
  let saved = false;
  try {
    const pair = await api(`/api/v1/session/reviews/${encodeURIComponent(state.currentPair.pair_id)}`, {
      method: "PUT",
      body: JSON.stringify({ review }),
    });
    renderPairDetail(pair);
    await loadResults();
    saved = true;
    announce("Human review saved for this local session.");
  } catch (error) {
    showDialogError(error, saveButton);
  } finally {
    setBusy(false);
  }
  if (saved) element("close-dialog").focus();
}

async function createSession(event) {
  event.preventDefault();
  clearError();
  setBusy(true, "Preparing catalogues", "Validating bytes, encoding, and CSV structure locally.");
  let prepared = false;
  try {
    const request = await sessionRequest();
    const payload = await api(API.session, { method: "POST", body: JSON.stringify(request) });
    state.sessionId = payload.session_id;
    renderMapping(payload);
    prepared = true;
    announce("Both catalogues are valid. Map their columns.");
  } catch (error) {
    showError(error, element("prepare-button"));
  } finally {
    setBusy(false);
  }
  if (prepared) element("mapping-title").focus();
}

async function runMatcher(event) {
  event.preventDefault();
  clearError();
  setBusy(true, "Running the matcher", "Comparing bounded candidate pairs with the frozen scoring policy.");
  let resolved = false;
  try {
    state.mapping = collectMapping();
    await api(API.mapping, { method: "PUT", body: JSON.stringify({ mapping: state.mapping }) });
    setStep(3);
    const payload = await api(API.run, { method: "POST", body: JSON.stringify({}) });
    renderResults(payload);
    resolved = true;
    announce(`${payload.total} pairs resolved by the matcher.`);
  } catch (error) {
    showError(error, element("run-button"));
  } finally {
    setBusy(false);
  }
  if (resolved) element("results-title").focus();
}

function initialState() {
  state.sessionId = null;
  state.previews = null;
  state.mapping = null;
  state.counts = null;
  state.decision = "";
  state.query = "";
  state.page = 1;
  state.total = 0;
  state.currentPair = null;
  comparisonReturnPairId = null;
  if (element("pair-dialog").open) element("pair-dialog").close();
  element("catalogue-form").reset();
  element("decision-filter").value = "";
  element("query-filter").value = "";
  element("input-panel").hidden = false;
  element("mapping-panel").hidden = true;
  element("results-panel").hidden = true;
  element("pair-list").replaceChildren();
  element("pair-detail").replaceChildren();
  element("catalogue-previews").replaceChildren();
  element("mapping-tables").replaceChildren();
  element("decision-counts").replaceChildren();
  clearError();
  updateSourceFields();
  setStep(1);
}

async function resetSession() {
  const oldSession = state.sessionId;
  setBusy(true, "Resetting the workbench", "Discarding only this temporary session.");
  try {
    if (oldSession) {
      try {
        await api(API.session, { method: "DELETE" });
      } catch (error) {
        if (error?.code !== "SESSION_NOT_FOUND") throw error;
      }
    }
    initialState();
    element("page-title").focus();
    announce("The local session was reset.");
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

async function exportSession(event) {
  event.preventDefault();
  setBusy(true, "Preparing the export", "Neutralizing spreadsheet formulas in this session's results.");
  try {
    const response = await fetch(API.export, {
      headers: { "X-ERW-Session": state.sessionId },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error("The current session could not be exported.");
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = make("a", { attrs: { href: url, download: "entity-resolution-session.csv" } });
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    announce("The current session export is ready.");
  } catch (error) {
    showError(error);
  } finally {
    setBusy(false);
  }
}

function filterSubmit(event) {
  event.preventDefault();
  state.decision = element("decision-filter").value;
  state.query = element("query-filter").value.trim();
  state.page = 1;
  loadResults({ focus: true });
}

function clearFilters() {
  element("decision-filter").value = "";
  element("query-filter").value = "";
  state.decision = "";
  state.query = "";
  state.page = 1;
  loadResults({ focus: true });
}

function wireEvents() {
  for (const radio of document.querySelectorAll('input[name="source-kind"]')) {
    radio.addEventListener("change", updateSourceFields);
  }
  element("catalogue-form").addEventListener("submit", createSession);
  element("mapping-form").addEventListener("submit", runMatcher);
  element("back-to-input").addEventListener("click", resetSession);
  element("dismiss-error").addEventListener("click", () => {
    const target = pageErrorReturnTarget;
    pageErrorReturnTarget = null;
    dismissError("error-panel", target);
  });
  element("dismiss-dialog-error").addEventListener("click", () => {
    const target = dialogErrorReturnTarget;
    dialogErrorReturnTarget = null;
    dismissError("dialog-error-panel", target);
  });
  element("filter-form").addEventListener("submit", filterSubmit);
  element("clear-filters").addEventListener("click", clearFilters);
  element("empty-clear").addEventListener("click", clearFilters);
  element("reset-button").addEventListener("click", resetSession);
  element("export-link").addEventListener("click", exportSession);
  element("close-dialog").addEventListener("click", () => {
    element("dialog-error-panel").hidden = true;
    element("pair-dialog").close();
  });
  element("pair-dialog").addEventListener("click", (event) => {
    if (event.target === element("pair-dialog")) element("pair-dialog").close();
  });
  element("pair-dialog").addEventListener("keydown", keepComparisonFocus);
  element("pair-dialog").addEventListener("close", restoreComparisonFocus);
  element("previous-page").addEventListener("click", () => { state.page -= 1; loadResults({ focus: true }); });
  element("next-page").addEventListener("click", () => { state.page += 1; loadResults({ focus: true }); });
}

async function loadFixtures() {
  try {
    await api(API.fixtures);
  } catch (error) {
    showError(error);
    element("prepare-button").disabled = true;
  }
}

wireEvents();
initialState();
loadFixtures();
