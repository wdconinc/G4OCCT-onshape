// SPDX-License-Identifier: LGPL-2.1-or-later
// Copyright (C) 2026 G4OCCT Contributors
//
// G4OCCT Onshape iframe frontend.
//
// This script runs inside the Onshape iframe tab.  The server injects a
// <script> block that sets window.G4OCCT_CONTEXT before this file loads:
//
//   window.G4OCCT_CONTEXT = {
//     documentId, workspaceId, elementId, userName, userEmail
//   };
//
// All Onshape API calls go through the App Server (same-origin), so OAuth
// tokens are never exposed to this script.

"use strict";

// ── Context ────────────────────────────────────────────────────────────────
const ctx = window.G4OCCT_CONTEXT || {};

// ── DOM helpers ────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const text = (id, val) => { const el = $(id); if (el) el.textContent = val; };

// ── Initialise context panel ───────────────────────────────────────────────
function initContextPanel() {
  text("ctx-documentId", ctx.documentId || "—");
  text("ctx-workspaceId", ctx.workspaceId || "—");
  text("ctx-elementId", ctx.elementId || "—");

  if (ctx.userName) {
    $("user-info").textContent = ctx.userName;
  }

  if (ctx.documentId && ctx.workspaceId && ctx.elementId) {
    fetchElementMetadata();
  } else {
    text("ctx-elementName", "No document context");
    text("ctx-elementType", "—");
  }
}

// ── Fetch element metadata from the App Server proxy ──────────────────────
async function fetchElementMetadata() {
  try {
    const params = new URLSearchParams({
      documentId: ctx.documentId,
      workspaceId: ctx.workspaceId,
      elementId: ctx.elementId,
    });
    const resp = await fetch(`/api/element/metadata?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    text("ctx-elementName", data.name || data.elementId || "—");
    text("ctx-elementType", data.elementType || "—");
    // Pre-select element type in the form if we have it
    const elTypeSelect = $("element-type-select");
    const elType = (data.elementType || "").toLowerCase();
    if (elTypeSelect && elType.includes("assembly")) {
      elTypeSelect.value = "assembly";
    }
  } catch (err) {
    text("ctx-elementName", `Error: ${err.message}`);
  }
}

// ── Job submission ─────────────────────────────────────────────────────────
$("sim-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const btn = $("submit-btn");
  btn.disabled = true;
  btn.textContent = "Submitting…";

  const body = {
    documentId: ctx.documentId,
    workspaceId: ctx.workspaceId,
    elementId: ctx.elementId,
    elementType: form.elements["elementType"].value,
    simulationConfig: {
      type: form.elements["type"].value,
      particleType: form.elements["particleType"].value,
      nEvents: parseInt(form.elements["nEvents"].value, 10),
    },
  };

  try {
    const resp = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      throw new Error(err.detail || resp.statusText);
    }
    const job = await resp.json();
    showNotification(`Job submitted (${job.id.slice(0, 8)}…)`, "success");
    loadJobs();
    pollJob(job.id);
  } catch (err) {
    showNotification(`Submission failed: ${err.message}`, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Simulation";
  }
});

// ── Load jobs list ─────────────────────────────────────────────────────────
async function loadJobs() {
  const container = $("jobs-list");
  try {
    const resp = await fetch("/api/jobs");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const jobs = await resp.json();
    renderJobList(jobs);
  } catch (err) {
    container.innerHTML = `<p class="empty-state">Error loading jobs: ${err.message}</p>`;
  }
}

function renderJobList(jobs) {
  const container = $("jobs-list");
  if (!jobs.length) {
    container.innerHTML = '<p class="empty-state">No jobs yet.</p>';
    return;
  }
  container.innerHTML = jobs.map((job) => {
    const created = new Date(job.created_at).toLocaleString();
    const simType = (() => {
      try { return JSON.parse(job.sim_config).type || ""; } catch { return ""; }
    })();
    return `
      <div class="job-item" data-job-id="${escHtml(job.id)}" onclick="showJobResult('${escHtml(job.id)}')">
        <div>
          <div class="job-id">${escHtml(job.id)}</div>
          <div class="job-meta">${escHtml(simType)} &nbsp;·&nbsp; ${escHtml(created)}</div>
        </div>
        <span class="job-status status-${escHtml(job.status)}">${escHtml(job.status)}</span>
      </div>`;
  }).join("");
}

// ── Show / poll a specific job ─────────────────────────────────────────────
async function showJobResult(jobId) {
  const panel = $("results-panel");
  const content = $("results-content");
  panel.classList.remove("hidden");

  try {
    const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const job = await resp.json();
    let html = `<p><strong>Job:</strong> <code>${escHtml(job.id)}</code></p>`;
    html += `<p><strong>Status:</strong> <span class="job-status status-${escHtml(job.status)}">${escHtml(job.status)}</span></p>`;
    if (job.results) {
      const results = typeof job.results === "string" ? JSON.parse(job.results) : job.results;
      html += `<pre>${escHtml(JSON.stringify(results, null, 2))}</pre>`;
    }
    content.innerHTML = html;
  } catch (err) {
    content.innerHTML = `<p class="empty-state">Error loading result: ${err.message}</p>`;
  }
}

// ── Long-poll for job completion ───────────────────────────────────────────
async function pollJob(jobId, intervalMs = 3000, maxAttempts = 100) {
  for (let i = 0; i < maxAttempts; i++) {
    await sleep(intervalMs);
    try {
      const resp = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!resp.ok) break;
      const job = await resp.json();
      loadJobs(); // Refresh the list
      if (job.status === "complete" || job.status === "failed") {
        showJobResult(jobId);
        break;
      }
    } catch {
      break;
    }
  }
}

// ── Refresh button ─────────────────────────────────────────────────────────
$("refresh-jobs-btn").addEventListener("click", loadJobs);

// ── Utilities ──────────────────────────────────────────────────────────────
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

let _notifTimer;
function showNotification(msg, type = "info") {
  let notif = document.querySelector(".notification");
  if (!notif) {
    notif = document.createElement("div");
    notif.className = "notification";
    Object.assign(notif.style, {
      position: "fixed",
      top: "16px",
      right: "16px",
      padding: "10px 18px",
      borderRadius: "8px",
      fontFamily: "inherit",
      fontSize: "0.875rem",
      fontWeight: "600",
      zIndex: "9999",
      boxShadow: "0 2px 8px rgba(0,0,0,0.2)",
      transition: "opacity 0.3s",
    });
    document.body.appendChild(notif);
  }
  notif.textContent = msg;
  notif.style.background = type === "success" ? "#1d8348" : type === "error" ? "#a93226" : "#1a6eb0";
  notif.style.color = "#fff";
  notif.style.opacity = "1";
  clearTimeout(_notifTimer);
  _notifTimer = setTimeout(() => { notif.style.opacity = "0"; }, 4000);
}

// ── Boot ───────────────────────────────────────────────────────────────────
initContextPanel();
loadJobs();
