// Minimal vanilla JS, no framework/build step. The only reason JS exists
// here at all is that a plain synchronous form POST would leave the page
// frozen for however long the MCP call takes (4-80s, cache hit vs. live
// agent call) with no feedback that anything is happening.

const form = document.getElementById("query-form");
const queryInput = document.getElementById("query");
const submitBtn = document.getElementById("submit-btn");
const chat = document.getElementById("chat");
const chips = document.getElementById("chips");
const filterCount = document.getElementById("filter-count");

function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function selectedSources() {
  return Array.from(form.ownerDocument.querySelectorAll('input[name="sources"]:checked')).map((el) => el.value);
}

function selectedOperation() {
  return form.ownerDocument.querySelector('input[name="operation"]:checked').value;
}

// The badge counts only *narrowing* choices -- picking sources narrows the
// query, but the ask/search toggle always has one value selected and so
// isn't a "filter" in the sense the badge communicates.
function updateFilterCount() {
  filterCount.textContent = String(selectedSources().length);
}

document.querySelectorAll('input[name="sources"]').forEach((el) => {
  el.addEventListener("change", updateFilterCount);
});
updateFilterCount();

chips.addEventListener("click", (event) => {
  const chip = event.target.closest(".chip");
  if (!chip) return;
  queryInput.value = chip.textContent.trim();
  queryInput.focus();
});

function appendMessage(role, innerHtml, extraClass = "") {
  const wrapper = document.createElement("div");
  wrapper.className = `msg ${role}`;
  const avatar = role === "assistant" ? '<div class="avatar">K2</div>' : "";
  wrapper.innerHTML = `${avatar}<div class="bubble ${extraClass}">${innerHtml}</div>`;
  chat.appendChild(wrapper);
  chat.scrollTop = chat.scrollHeight;
  return wrapper.querySelector(".bubble");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const query = queryInput.value.trim();
  if (!query) return;

  const operation = selectedOperation();
  const sources = selectedSources();

  appendMessage("user", escapeHtml(query));
  queryInput.value = "";
  submitBtn.disabled = true;

  const pendingBubble = appendMessage(
    "assistant",
    "Consultando a2k-box... puede tardar desde unos segundos (respuesta en caché) hasta cerca de un minuto (consulta en vivo al proveedor).",
    "pending"
  );

  const t0 = performance.now();
  try {
    const response = await fetch(`/api/${operation}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, sources }),
    });
    const envelope = await response.json();
    const elapsedS = ((performance.now() - t0) / 1000).toFixed(1);
    pendingBubble.classList.remove("pending");
    pendingBubble.innerHTML = render(envelope, operation, elapsedS);
  } catch (err) {
    pendingBubble.classList.remove("pending");
    pendingBubble.innerHTML = `<div class="error-box"><strong>ERROR DE RED</strong><p>${escapeHtml(err.message || err)}</p></div>`;
  } finally {
    submitBtn.disabled = false;
    chat.scrollTop = chat.scrollHeight;
    queryInput.focus();
  }
});

function render(envelope, operation, elapsedS) {
  if (!envelope.ok) {
    const err = envelope.error || {};
    return `
      <div class="error-box">
        <strong>${escapeHtml(err.code || "ERROR")}</strong>
        <p>${escapeHtml(err.message || "La consulta no se ha podido completar.")}</p>
      </div>`;
  }

  const citations = envelope.citations || [];
  const citationById = Object.fromEntries(citations.map((c) => [c.id, c]));

  const parts = [];
  parts.push(renderMeta(envelope, elapsedS));

  if (operation === "ask") {
    parts.push(renderAnswer(envelope));
    parts.push(renderClaims(envelope.claims || [], citationById));
  } else {
    parts.push(renderPassages(envelope.passages || [], citationById));
  }

  parts.push(renderCitations(citations));

  if (envelope.conflicts && envelope.conflicts.length > 0) {
    parts.push(renderConflicts(envelope.conflicts));
  }

  parts.push(renderFreshness(envelope.freshness, envelope.audit));

  if (envelope.toolCalls && envelope.toolCalls.length > 0) {
    parts.push(renderToolCalls(envelope.toolCalls));
  }

  return parts.filter(Boolean).join("\n");
}

function renderMeta(envelope, elapsedS) {
  const audit = envelope.audit || {};
  const grounding = envelope.grounding || {};
  const usage = envelope.usage || {};
  const isCacheHit = (audit.decisionReason || "").toLowerCase().includes("cache");

  const badges = [];
  if (isCacheHit) badges.push('<span class="badge cache-badge">servido desde caché</span>');
  if (grounding.groundedRatio !== undefined && grounding.groundedRatio !== null) {
    badges.push(`<span class="badge">fundamentación: ${(grounding.groundedRatio * 100).toFixed(0)}%</span>`);
  }
  badges.push(`<span class="muted">${escapeHtml(elapsedS)}s</span>`);
  if (usage.totalTokens) badges.push(`<span class="muted">${usage.totalTokens} tok</span>`);

  const reason = audit.decisionReason
    ? `<div class="decision-reason ${isCacheHit ? "is-cache" : ""}">${escapeHtml(audit.decisionReason)}</div>`
    : "";

  return `<div class="meta-row">${badges.join("")}</div>${reason}`;
}

function renderAnswer(envelope) {
  if (!envelope.answer) {
    return '<p class="answer-text muted"><em>Sin respuesta sintetizada -- revisa afirmaciones y citas.</em></p>';
  }
  return `<p class="answer-text">${escapeHtml(envelope.answer)}</p>`;
}

function renderClaims(claims, citationById) {
  if (claims.length === 0) return "";
  const items = claims
    .map((claim) => {
      const cites = (claim.citationIds || []).map((id) => citationLink(citationById[id])).join(" ");
      return `
        <li>
          <span class="status-badge status-${escapeHtml((claim.status || "").toLowerCase())}">${escapeHtml(claim.status || "")}</span>
          ${escapeHtml(claim.text)}
          <span class="cite-refs">${cites}</span>
        </li>`;
    })
    .join("");
  return `<div class="result-block"><h3 class="block-title">Afirmaciones</h3><ul>${items}</ul></div>`;
}

function renderPassages(passages, citationById) {
  if (passages.length === 0) {
    return '<div class="result-block"><p class="muted"><em>No se han devuelto pasajes.</em></p></div>';
  }
  const items = passages
    .map((p) => {
      const cites = (p.citationIds || []).map((id) => citationLink(citationById[id])).join(" ");
      return `<li>${escapeHtml(p.text)} <span class="cite-refs">${cites}</span></li>`;
    })
    .join("");
  return `<div class="result-block"><h3 class="block-title">Pasajes</h3><ul>${items}</ul></div>`;
}

function citationLink(citation) {
  if (!citation) return "";
  const label = escapeHtml(citation.id);
  return citation.sourceUrl
    ? `<a href="${escapeHtml(citation.sourceUrl)}" target="_blank" rel="noopener">[${label}]</a>`
    : `<span title="${escapeHtml(citation.title || citation.documentId || "")}">[${label}]</span>`;
}

function renderCitations(citations) {
  if (citations.length === 0) return "";
  const items = citations
    .map((c) => {
      const title = c.title || c.documentId || c.id;
      const link = c.sourceUrl
        ? `<a href="${escapeHtml(c.sourceUrl)}" target="_blank" rel="noopener">${escapeHtml(title)}</a>`
        : escapeHtml(title);
      return `<li><strong>[${escapeHtml(c.id)}]</strong> ${link}</li>`;
    })
    .join("");
  return `<div class="result-block citations"><h3 class="block-title">Citas</h3><ul>${items}</ul></div>`;
}

function renderConflicts(conflicts) {
  const items = conflicts
    .map(
      (c) => `
      <li>
        <strong>${escapeHtml(c.nature || "conflicto")}</strong>
        <div>"${escapeHtml(c.thisPosition)}" frente a "${escapeHtml(c.otherPosition)}"</div>
        <div class="muted">${escapeHtml(c.assessment || "")}</div>
      </li>`
    )
    .join("");
  return `<div class="result-block"><h3 class="block-title">Conflictos entre fuentes</h3><ul>${items}</ul></div>`;
}

function renderFreshness(freshness, audit) {
  if (!freshness) return "";
  const staleBadge = freshness.stale
    ? '<span class="badge stale-badge">DATO CADUCADO</span>'
    : '<span class="badge fresh-badge">dato vigente</span>';
  const requestId = (audit || {}).requestId
    ? `<span class="muted">requestId: ${escapeHtml(audit.requestId)}</span>`
    : "";
  return `
    <div class="result-block">
      <div class="meta-row">
        ${staleBadge}
        <span class="muted">obtenido: ${escapeHtml(freshness.retrievedAt || "")}</span>
        ${requestId}
      </div>
    </div>`;
}

function renderToolCalls(toolCalls) {
  const rows = toolCalls
    .map(
      (tc) => `
      <tr>
        <td>${escapeHtml(tc.vendor || "")}</td>
        <td>${escapeHtml(tc.toolName || "")}</td>
        <td>${escapeHtml(tc.status || "")}</td>
        <td><pre>${escapeHtml(JSON.stringify(tc.input || {}))}</pre></td>
      </tr>`
    )
    .join("");
  return `
    <details class="tool-calls">
      <summary>Llamadas a herramientas (${toolCalls.length})</summary>
      <table>
        <thead><tr><th>fuente</th><th>herramienta</th><th>estado</th><th>entrada</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </details>`;
}
