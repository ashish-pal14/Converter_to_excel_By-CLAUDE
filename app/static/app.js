(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    sheet: $("sheet"), drop: $("drop"), file: $("file"), title: $("drop-title"), hint: $("drop-hint"),
    status: $("status"), statusFile: $("status-file"), statusMsg: $("status-msg"), bar: $("bar"), barFill: $("bar-fill"),
    settings: $("settings"), pages: $("pages"), lang: $("ocr_lang"), password: $("password"),
    passwordField: $("password-field"), forceOcr: $("force_ocr"),
    result: $("result"), resultTitle: $("result-title"), summary: $("result-summary"), download: $("download"),
    again: $("again"), notes: $("page-notes"), tabs: $("tabs"), preview: $("preview"), legend: $("legend"),
    error: $("error"), errorMsg: $("error-msg"), retry: $("retry"), ttl: $("ttl"),
  };
  const LANGS = { eng: "English", deu: "German", fra: "French", spa: "Spanish", ita: "Italian",
    por: "Portuguese", nld: "Dutch", ara: "Arabic", hin: "Hindi", chi_sim: "Chinese (simplified)",
    chi_tra: "Chinese (traditional)", jpn: "Japanese", kor: "Korean", rus: "Russian", tur: "Turkish",
    pol: "Polish", tam: "Tamil", tel: "Telugu", ben: "Bengali", urd: "Urdu", mar: "Marathi" };
  const FILE_LABEL = { xlsx: "Download Excel file", csv: "Download CSV files (.zip)", json: "Download JSON file" };

  let config = { max_file_mb: 50 };
  let lastFile = null;
  let pollTimer = null;
  let currentJob = null;

  // ---------------------------------------------------------------- setup
  fetch("/api/config").then((r) => r.json()).then((c) => {
    config = c;
    els.hint.textContent = `PDF, PNG, JPG or TIFF, up to ${c.max_file_mb} MB`;
    els.lang.innerHTML = c.ocr_languages
      .map((l) => `<option value="${l}" ${l === "eng" ? "selected" : ""}>${LANGS[l] || l}</option>`).join("");
    const h = c.job_ttl_minutes;
    els.ttl.textContent = h % 60 === 0 ? `after ${h / 60 === 1 ? "an hour" : h / 60 + " hours"}` : `after ${h} minutes`;
  }).catch(() => {});

  const radio = (name) => document.querySelector(`input[name=${name}]:checked`).value;

  // ---------------------------------------------------------------- file picking
  els.file.addEventListener("change", () => { if (els.file.files[0]) start(els.file.files[0]); });
  els.drop.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); els.file.click(); }
  });
  ["dragenter", "dragover"].forEach((t) => els.sheet.addEventListener(t, (e) => {
    e.preventDefault(); els.sheet.classList.add("dragging");
  }));
  ["dragleave", "drop"].forEach((t) => els.sheet.addEventListener(t, (e) => {
    e.preventDefault(); els.sheet.classList.remove("dragging");
  }));
  els.sheet.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) start(f);
  });
  els.again.addEventListener("click", reset);
  els.retry.addEventListener("click", () => (lastFile ? start(lastFile) : reset()));
  els.password.addEventListener("input", () => els.passwordField.classList.remove("needs"));

  // ---------------------------------------------------------------- flow
  function reset() {
    clearTimeout(pollTimer);
    if (currentJob) fetch(`/api/jobs/${currentJob}`, { method: "DELETE" }).catch(() => {});
    currentJob = null;
    els.result.hidden = true; els.error.hidden = true; els.status.hidden = true;
    els.sheet.classList.remove("busy");
    els.file.value = "";
    els.drop.focus();
  }

  function setProgress(fraction, msg) {
    const pct = Math.round(fraction * 100);
    els.barFill.style.width = pct + "%";
    els.bar.setAttribute("aria-valuenow", pct);
    if (msg) els.statusMsg.textContent = msg;
  }

  function fail(message) {
    clearTimeout(pollTimer);
    els.sheet.classList.remove("busy");
    els.status.hidden = true;
    els.result.hidden = true;
    els.error.hidden = false;
    els.errorMsg.textContent = message;
    if (/password/i.test(message)) {
      els.settings.open = true;
      els.passwordField.classList.add("needs");
      els.password.focus();
      els.retry.textContent = "Convert with this password";
    } else {
      els.retry.textContent = "Try again";
    }
  }

  function start(file) {
    lastFile = file;
    els.error.hidden = true; els.result.hidden = true;
    if (file.size > config.max_file_mb * 1024 * 1024) {
      return fail(`This file is ${(file.size / 1048576).toFixed(1)} MB. The limit is ${config.max_file_mb} MB.`);
    }
    els.sheet.classList.add("busy");
    els.status.hidden = false;
    els.statusFile.textContent = file.name;
    setProgress(0, "Uploading");

    const form = new FormData();
    form.append("file", file);
    form.append("output", radio("output"));
    form.append("layout", radio("layout"));
    form.append("date_order", radio("date_order"));
    form.append("ocr_lang", els.lang.value || "eng");
    form.append("force_ocr", els.forceOcr.checked ? "true" : "false");
    form.append("convert_types", document.getElementById("as_text").checked ? "false" : "true");
    if (els.pages.value.trim()) form.append("pages", els.pages.value.trim());
    if (els.password.value) form.append("password", els.password.value);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) setProgress(0.25 * (e.loaded / e.total), "Uploading");
    };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status >= 400) return fail(body.error || `Upload failed (${xhr.status}).`);
      currentJob = body.id;
      poll(body.id);
    };
    xhr.onerror = () => fail("The upload did not reach the server. Check your connection and try again.");
    xhr.send(form);
  }

  function poll(id) {
    fetch(`/api/jobs/${id}`).then((r) => r.json().then((b) => ({ ok: r.ok, b })))
      .then(({ ok, b }) => {
        if (!ok) return fail(b.error || "The conversion was lost. Try again.");
        if (b.status === "failed") return fail(b.error || "This file could not be converted.");
        if (b.status === "done") return finish(b);
        setProgress(0.25 + 0.75 * b.progress, b.message);
        pollTimer = setTimeout(() => poll(id), 600);
      })
      .catch(() => { pollTimer = setTimeout(() => poll(id), 1500); });
  }

  // ---------------------------------------------------------------- result
  function finish(job) {
    setProgress(1, "Done");
    els.sheet.classList.remove("busy");
    els.status.hidden = true;
    const s = job.summary;
    const exact = s.mode === "exact";
    const parts = exact
      ? [`${s.pages} ${s.pages === 1 ? "page" : "pages"} copied`]
      : [`${s.pages} ${s.pages === 1 ? "page" : "pages"}`, `${s.tables} ${s.tables === 1 ? "table" : "tables"} found`];
    if (s.scanned_pages) parts.push(`${s.scanned_pages} read from scans`);
    els.summary.textContent = parts.join(", ") + ".";
    const tx = s.transactions;
    if (tx) {
      const money = (v) => v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      els.summary.textContent += ` ${tx.count} transactions: debits ${money(tx.debits)}, credits ${money(tx.credits)}.`;
      tx.checks.forEach((c) => { els.summary.textContent += ` ${c}.`; });
    }
    els.resultTitle.textContent = exact ? "Your copy is ready"
      : s.transactions ? "Your statement is ready"
      : s.tables ? "Your spreadsheet is ready" : "No tables found, text was extracted";
    els.download.href = `/api/jobs/${job.id}/download`;
    els.download.textContent = FILE_LABEL[job.output_name.split(".").pop()] || "Download file";

    els.notes.innerHTML = "";
    s.page_details.forEach((p) => {
      const li = document.createElement("li");
      const kind = { digital: "text", scanned: "scan", empty: "blank", error: "unreadable" }[p.type] || p.type;
      let text = `Page ${p.page}: ${kind}`;
      if (p.confidence != null) text += `, ${Math.round(p.confidence)}% legible`;
      li.textContent = text;
      if (p.notes.length) li.title = p.notes.join("; ");
      if (p.type === "scanned") li.classList.add("scan");
      if (p.type === "error" || (p.confidence != null && p.confidence < 60)) li.classList.add("warn");
      els.notes.appendChild(li);
    });
    if (s.page_details.length > 40) els.notes.hidden = true;

    fetch(`/api/jobs/${job.id}/preview`).then((r) => r.json()).then((data) => {
      renderTabs(data.sheets);
      els.result.hidden = false;
      els.download.focus();
    }).catch(() => { els.result.hidden = false; });
  }

  function renderTabs(sheets) {
    els.tabs.innerHTML = "";
    sheets.forEach((sheet, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "tab");
      b.textContent = sheet.name;
      b.addEventListener("click", () => select(i));
      els.tabs.appendChild(b);
    });
    const select = (i) => {
      [...els.tabs.children].forEach((b, k) => b.setAttribute("aria-selected", k === i ? "true" : "false"));
      renderSheet(sheets[i]);
    };
    if (sheets.length) select(0);
    els.legend.hidden = !sheets.some((s) => s.rows.some((r) => r.some((c) => c.f === 1)));
  }

  function colName(n) {
    let s = "";
    for (n += 1; n > 0; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
    return s;
  }

  function renderSheet(sheet) {
    const width = Math.max(1, ...sheet.rows.map((r) => r.length));
    const esc = (t) => t.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    let html = "<thead><tr><th></th>";
    for (let c = 0; c < width; c++) html += `<th>${colName(c)}</th>`;
    html += "</tr></thead><tbody>";
    sheet.rows.forEach((row, r) => {
      html += `<tr><td class="rn">${r + 1}</td>`;
      for (let c = 0; c < width; c++) {
        const cell = row[c] || { v: "", f: 0 };
        const cls = [cell.n ? "num" : "", cell.f === 1 ? "low" : "", cell.f === 2 ? "hdr" : "", cell.f === 3 ? "title" : ""]
          .filter(Boolean).join(" ");
        html += `<td class="${cls}" title="${esc(cell.v)}">${esc(cell.v)}</td>`;
      }
      html += "</tr>";
    });
    if (sheet.total_rows > sheet.rows.length) {
      html += `<tr><td class="more" colspan="${width + 1}">Showing the first ${sheet.rows.length} of ${sheet.total_rows} rows. The download has them all.</td></tr>`;
    }
    els.preview.innerHTML = html + "</tbody>";
  }
})();
