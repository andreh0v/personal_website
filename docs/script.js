// Shared helpers + per-page renderers. No build step: fetches JSON/CSV/Markdown at
// runtime and renders client-side.

function fmtPct(n) {
  if (n === null || n === undefined) return "—";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(1)}%`;
}

function gainClass(n) {
  if (n === null || n === undefined) return "";
  return n >= 0 ? "gain" : "loss";
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// Renders a constrained inline markdown subset: **bold** and [text](url).
function mdInline(str) {
  let html = escapeHtml(str);
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  return html;
}

// Minimal CSV parser handling quoted fields with embedded commas.
function parseCSV(text) {
  const rows = [];
  let row = [], field = "", inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; }
        else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else field += c;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  const header = rows.shift();
  return rows.map((r) => Object.fromEntries(header.map((h, idx) => [h, r[idx]])));
}

async function fetchJSON(path) {
  const res = await fetch(path);
  return res.json();
}

async function fetchText(path) {
  const res = await fetch(path);
  return res.text();
}

// ---------- Portfolio page ----------

async function renderPortfolioPage() {
  const portfolio = await fetchJSON("data/portfolio.json");
  const history = await fetchJSON("data/history.json");

  document.getElementById("summary-row").innerHTML = `
    <div class="summary-cell"><div class="label">Holdings</div><div class="value">${portfolio.holdings.length}</div></div>
    <div class="summary-cell"><div class="label">Blended unrealized return</div><div class="value ${gainClass(portfolio.blended_unrealized_return_pct)}">${fmtPct(portfolio.blended_unrealized_return_pct)}</div></div>
    <div class="summary-cell"><div class="label">Blended realized return</div><div class="value ${gainClass(portfolio.realized_gains.blended_return_pct)}">${fmtPct(portfolio.realized_gains.blended_return_pct)}</div></div>
  `;

  const compPalette = ["#1f4d3f", "#7f9c8f", "#a3702c", "#4a5164", "#c2b280", "#8a2e2e", "#5b7a99", "#9fae7e", "#c9a66b", "#6b8e7f"];
  new Chart(document.getElementById("composition-chart"), {
    type: "doughnut",
    data: {
      labels: portfolio.composition.map((c) => c.label),
      datasets: [{
        data: portfolio.composition.map((c) => c.pct),
        backgroundColor: portfolio.composition.map((_, i) => compPalette[i % compPalette.length]),
        borderColor: "#faf9f6",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${ctx.parsed.toFixed(1)}%` } },
      },
    },
  });

  document.querySelector("#composition-table tbody").innerHTML = portfolio.composition.map((c) => `
    <tr>
      <td>${escapeHtml(c.label)}</td>
      <td class="num">${c.pct.toFixed(1)}%</td>
    </tr>
  `).join("");
  document.getElementById("composition-note").textContent =
    "Includes all holdings and cash/savings accounts. Small holdings are grouped once below "
    + "a threshold rather than listed individually.";

  new Chart(document.getElementById("value-chart"), {
    type: "line",
    data: {
      labels: history.map((h) => h.date),
      datasets: [{
        label: "Blended unrealized return",
        data: history.map((h) => h.unrealized_return_pct),
        borderColor: "#1f4d3f",
        backgroundColor: "rgba(31,77,63,0.08)",
        fill: true,
        tension: 0.15,
        pointRadius: 0,
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        y: { ticks: { callback: (v) => `${Number(v).toFixed(1)}%` } },
      },
    },
  });

  const tbody = document.querySelector("#holdings-table tbody");
  tbody.innerHTML = portfolio.holdings.map((h) => `
    <tr>
      <td>${escapeHtml(h.name)}${h.flagged_manual ? '<span class="badge">manually updated</span>' : ""}</td>
      <td>${escapeHtml(h.account)}</td>
      <td class="num">${h.pct_of_portfolio.toFixed(1)}%</td>
      <td class="num ${gainClass(h.unrealized_gain_pct)}">${fmtPct(h.unrealized_gain_pct)}</td>
    </tr>
  `).join("");

  const paletteBase = ["#1f4d3f", "#7f9c8f", "#a3702c", "#4a5164", "#c2b280", "#8a2e2e", "#5b7a99", "#9fae7e"];
  new Chart(document.getElementById("allocation-chart"), {
    type: "doughnut",
    data: {
      labels: portfolio.holdings.map((h) => h.name),
      datasets: [{
        data: portfolio.holdings.map((h) => h.pct_of_portfolio || 0),
        backgroundColor: portfolio.holdings.map((_, i) => paletteBase[i % paletteBase.length]),
        borderColor: "#faf9f6",
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${ctx.parsed.toFixed(1)}%` } },
      },
    },
  });

  const cashTbody = document.querySelector("#cash-table tbody");
  cashTbody.innerHTML = portfolio.cash_accounts.map((c) => `
    <tr>
      <td>${escapeHtml(c.account)}${c.is_bsu ? '<span class="badge">BSU</span>' : ""}</td>
      <td class="num">${c.interest_rate_pct}%</td>
      <td>${c.tax_deductible ? "Yes" : "No"}</td>
    </tr>
  `).join("");

  const bsu = portfolio.bsu_tax_benefit;
  if (bsu && bsu.applicable) {
    document.getElementById("bsu-note").style.display = "block";
    document.getElementById("bsu-note").innerHTML = `
      <strong>BSU tax benefit</strong><br>${escapeHtml(bsu.note)}
    `;
  }

  document.getElementById("realized-row").innerHTML = portfolio.realized_gains.by_ticker.map((r) => `
    <div class="summary-cell">
      <div class="label">${escapeHtml(r.name)}</div>
      <div class="value ${gainClass(r.realized_return_pct)}" style="font-size:1.15rem;">${fmtPct(r.realized_return_pct)}</div>
      ${r.period_held ? `<div class="section-note" style="margin:4px 0 0;">${escapeHtml(r.period_held)}</div>` : ""}
    </div>
  `).join("") || '<div class="summary-cell"><div class="label">No realized sales yet</div></div>';
}

// ---------- Academics page ----------

async function renderAcademicsPage() {
  const csvText = await fetchText("data/grades.csv");
  const rows = parseCSV(csvText);

  const totalEcts = rows.reduce((sum, r) => sum + (parseFloat(r.credits_ects) || 0), 0);
  document.getElementById("total-ects").textContent = `${rows.length} courses · ${totalEcts} ECTS total`;

  const categories = new Map();
  for (const r of rows) {
    if (!categories.has(r.category)) categories.set(r.category, []);
    categories.get(r.category).push(r);
  }

  // Study abroad last, everything else in encounter order
  const order = [...categories.keys()].sort((a, b) => {
    const abroad = (c) => /abroad/i.test(c) ? 1 : 0;
    return abroad(a) - abroad(b);
  });

  const container = document.getElementById("course-groups");
  container.innerHTML = order.map((cat) => {
    const items = categories.get(cat);
    const ects = items.reduce((s, r) => s + (parseFloat(r.credits_ects) || 0), 0);
    const rowsHtml = items.map((r) => `
      <tr>
        <td>${escapeHtml(r.course_code)}</td>
        <td>${escapeHtml(r.course_name)}</td>
        <td>${escapeHtml(r.semester)}</td>
        <td class="num">${escapeHtml(r.credits_ects)}</td>
        <td class="num">${escapeHtml(r.grade)}</td>
      </tr>
    `).join("");
    return `
      <div class="category-group">
        <div class="category-title">${escapeHtml(cat)} <span class="ects-total">— ${ects} ECTS</span></div>
        <div class="table-scroll">
          <table>
            <thead><tr><th>Code</th><th>Course</th><th>Semester</th><th class="num">ECTS</th><th class="num">Grade</th></tr></thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
      </div>
    `;
  }).join("");
}

// ---------- Experience page ----------

function parseExperienceMarkdown(md) {
  const lines = md.split("\n");
  let contact = "";
  const sections = [];
  let current = null;
  let currentItem = null;

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (line.startsWith("# ")) continue;
    if (line.startsWith("Contact:")) { contact = line; continue; }
    if (line.startsWith("(")) continue;
    if (line.startsWith("## ")) {
      current = { title: line.slice(3).trim(), items: [] };
      sections.push(current);
      currentItem = null;
      continue;
    }
    if (!current) continue;
    if (/^- /.test(line)) {
      currentItem = { header: line.slice(2).trim(), details: [] };
      current.items.push(currentItem);
    } else if (/^\s+- /.test(line)) {
      if (currentItem) currentItem.details.push({ type: "bullet", text: line.trim().slice(2).trim() });
    } else if (/^\s+\S/.test(line)) {
      if (currentItem) currentItem.details.push({ type: "text", text: line.trim() });
    }
  }
  return { contact, sections };
}

async function renderExperiencePage() {
  const md = await fetchText("data/experience.md");
  const { contact, sections } = parseExperienceMarkdown(md);

  document.getElementById("contact-line").innerHTML = mdInline(contact.replace(/^Contact:\s*/, ""));

  const root = document.getElementById("experience-root");
  root.innerHTML = sections.map((section) => {
    const isSkills = /skills/i.test(section.title);
    const itemsHtml = section.items.map((item) => {
      const headerHtml = mdInline(item.header);
      if (isSkills) {
        return `<div class="timeline-item" style="border:none; padding:6px 0;">${headerHtml}</div>`;
      }
      const detailsHtml = item.details.map((d) => {
        if (d.type === "bullet") return `<li>${mdInline(d.text)}</li>`;
        return `<div class="meta" style="margin-top:-4px;">${mdInline(d.text)}</div>`;
      }).join("");
      const bulletDetails = item.details.filter((d) => d.type === "bullet");
      const textDetails = item.details.filter((d) => d.type === "text");
      return `
        <div class="timeline-item">
          <div class="role">${headerHtml}</div>
          ${textDetails.map((d) => `<div class="meta">${mdInline(d.text)}</div>`).join("")}
          ${bulletDetails.length ? `<ul>${bulletDetails.map((d) => `<li>${mdInline(d.text)}</li>`).join("")}</ul>` : ""}
        </div>
      `;
    }).join("");
    return `
      <div class="timeline-section">
        <h2>${escapeHtml(section.title)}</h2>
        ${itemsHtml}
      </div>
    `;
  }).join("");
}
