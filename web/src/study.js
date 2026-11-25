const OUTCOMES = {
  respiratory: "Respiratórias",
  pneumonia: "Pneumonia",
  asthma: "Asma",
  circulatory: "Circulatórias",
  negative_control: "Lesões (controle negativo)",
};
const AGES = { all: "todas as idades", "0-4": "0–4 anos", "60+": "60+ anos" };
const AGE_ORDER = ["all", "0-4", "60+"];

const f3 = (x) => x.toFixed(3).replace(".", ",");
const ci = (e) => `${f3(e.rr)} (${f3(e.lower)}–${f3(e.upper)})`;
const excludesOne = (e) => e && (e.lower > 1 || e.upper < 1);
const percent = (e) =>
  `${e.rr >= 1 ? "+" : "−"}${(Math.abs(e.rr - 1) * 100).toFixed(1).replace(".", ",")}%`;

export const studyPage = () =>
  `<div class="eyebrow">ESTUDO DE ASSOCIAÇÃO · PESQUISA, NÃO VALIDADO</div><h1>PM2,5 e internações.</h1><p>Quanto sobem as internações hospitalares nos dias em que o PM2,5 está mais alto. Cada região de saúde tem seu próprio modelo; os resultados são combinados por meta-análise de efeitos aleatórios. Diferente do painel, estes números vêm de dados reais do SIH/SUS.</p><p id="study-scope" class="study-muted">Carregando resultados…</p><div id="study-body"></div>`;

function sorted(strata) {
  return Object.keys(OUTCOMES).flatMap((o) =>
    AGE_ORDER.map((a) =>
      strata.find((s) => s.outcome === o && s.age_group === a),
    ).filter(Boolean),
  );
}

function forest(rows, step) {
  const W = 760,
    L = 250,
    R = 24,
    rowH = 30,
    gap = 14,
    top = 30,
    bottom = 40;
  const lo = 0.85,
    hi = 1.15;
  const x = (v) =>
    L +
    ((Math.log(Math.min(Math.max(v, lo), hi)) - Math.log(lo)) /
      (Math.log(hi) - Math.log(lo))) *
      (W - L - R);
  let y = top,
    previous = null,
    s = "";
  const items = [];
  for (const r of rows) {
    if (r.outcome !== previous) {
      if (previous) y += gap;
      s += `<text x="8" y="${y + 12}" class="study-group">${OUTCOMES[r.outcome]}</text>`;
      previous = r.outcome;
    }
    items.push([r, y + rowH / 2]);
    y += rowH;
  }
  const H = y + bottom;
  let grid = "";
  for (const t of [0.85, 0.9, 0.95, 1, 1.05, 1.1, 1.15]) {
    grid += `<line x1="${x(t)}" x2="${x(t)}" y1="${top - 8}" y2="${H - bottom + 4}" class="${t === 1 ? "study-null" : "study-grid"}"/><text x="${x(t)}" y="${H - bottom + 20}" class="study-tick" text-anchor="middle">${t.toFixed(2).replace(".", ",")}</text>`;
  }
  const mark = (e, cy, kind, label) => {
    if (!e) return "";
    const a = x(e.lower),
      b = x(e.upper),
      c = x(e.rr);
    let m = `<line x1="${a}" x2="${b}" y1="${cy}" y2="${cy}" class="study-${kind}-line"/>`;
    if (e.lower < lo)
      m += `<path d="M${a} ${cy}l6 -4v8z" class="study-${kind}"/>`;
    if (e.upper > hi)
      m += `<path d="M${b} ${cy}l-6 -4v8z" class="study-${kind}"/>`;
    const title = `<title>${label}: ${ci(e)}, ${Number(e.regions)} regiões</title>`;
    m +=
      kind === "era5"
        ? `<rect x="${c - 4.5}" y="${cy - 4.5}" width="9" height="9" class="study-era5">${title}</rect>`
        : `<circle cx="${c}" cy="${cy}" r="4" class="study-merra2">${title}</circle>`;
    return m;
  };
  for (const [r, cy] of items) {
    s += `<text x="22" y="${cy + 12}" class="study-age">${AGES[r.age_group]}</text>`;
    s += mark(r.main, cy + 3, "era5", "ERA5");
    s += mark(r.merra2, cy + 12, "merra2", "MERRA-2");
    s += `<text x="${L - 10}" y="${cy + 12}" class="study-tick" text-anchor="end">${r.main ? f3(r.main.rr) : "—"}</text>`;
  }
  s += `<text x="${L - 10}" y="${top - 12}" class="study-tick" text-anchor="end">RR ERA5</text>`;
  s += `<text x="${L + (W - L - R) / 2}" y="${H - 4}" class="study-tick" text-anchor="middle">RR para +${step} µg/m³ (escala log)</text>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Riscos relativos combinados por desfecho e idade, com meteorologia ERA5 e MERRA-2">${grid}${s}</svg>`;
}

function sensitivity(rows) {
  const cell = (e) =>
    e
      ? `<td class="${excludesOne(e) ? "study-excludes" : ""}" title="${Number(e.regions)} regiões">${ci(e)}</td>`
      : "<td>—</td>";
  return rows
    .filter((r) => r.main)
    .map(
      (r) =>
        `<tr><th scope="row">${OUTCOMES[r.outcome]}, ${AGES[r.age_group]}</th><td>${Number(r.main.regions)}</td>${cell(r.main)}${cell(r.hac)}${cell(r.lagged)}<td>${Math.round(r.main.i2 * 100)}%</td></tr>`,
    )
    .join("");
}

function shift(rows) {
  const pick = [
    ["negative_control", "60+"],
    ["circulatory", "60+"],
    ["negative_control", "all"],
    ["respiratory", "all"],
    ["asthma", "all"],
  ];
  const lo = 0.97,
    hi = 1.05;
  const px = (v) => ((Math.min(Math.max(v, lo), hi) - lo) / (hi - lo)) * 100;
  return (
    pick
      .map(([o, a]) => rows.find((r) => r.outcome === o && r.age_group === a))
      .filter((r) => r?.spec6 && r?.merra2)
      .map((r) => {
        const a6 = px(r.spec6.rr),
          a7 = px(r.merra2.rr);
        return `<div class="study-shift-row"><span>${OUTCOMES[r.outcome]}, ${AGES[r.age_group]} <b>${f3(r.spec6.rr)} → ${f3(r.merra2.rr)}</b></span><div class="study-bar" role="img" aria-label="versão 6: ${f3(r.spec6.rr)}; versão 7: ${f3(r.merra2.rr)}"><i class="study-one" style="left:${px(1)}%"></i><i class="study-link" style="left:${Math.min(a6, a7)}%;width:${Math.abs(a7 - a6)}%"></i><i class="study-dot study-v6" style="left:${a6}%"></i><i class="study-dot study-v7" style="left:${a7}%"></i></div></div>`;
      })
      .join("") +
    `<div class="study-shift-row"><span></span><div class="study-bar study-axis">${[0.97, 1, 1.05].map((v) => `<span style="left:${px(v)}%">${v.toFixed(2).replace(".", ",")}</span>`).join("")}</div></div>`
  );
}

const fmt = (v) => String(Number(v)).replace(".", ",");

export function renderStudy(study) {
  const step = fmt(study.contrast_ug_m3[1] - study.contrast_ug_m3[0]);
  const maxShift = Math.max(
    0,
    ...study.strata.flatMap((s) =>
      [s.hac, s.lagged]
        .filter((e) => e && s.main)
        .map((e) => Math.abs(e.rr - s.main.rr)),
    ),
  );
  study.regions = Number(study.regions);
  const commits = [
    ...new Set(
      study.sources
        .flatMap((s) => [...(s.region_commits || []), s.git_commit])
        .filter((c) => /^[0-9a-f]{7,40}$/.test(c))
        .map((c) => `<code>${c.slice(0, 7)}</code>`),
    ),
  ].join(", ");
  // Older pools only checked tracked files, so the claim is narrower for them.
  const clean = study.dirty_check_includes_untracked
    ? "todas as regiões geradas sem alterações locais no código."
    : "todas as regiões geradas sem alterações locais em arquivos versionados.";
  const rows = sorted(study.strata);
  const find = (o, a) =>
    rows.find((r) => r.outcome === o && r.age_group === a)?.main;
  const headline = [
    ["respiratory", "all", "internações respiratórias"],
    ["pneumonia", "0-4", "pneumonia, 0–4 anos"],
    ["asthma", "all", "asma"],
    ["negative_control", "all", "lesões (controle negativo)"],
  ]
    .map(([o, a, label]) => [find(o, a), label])
    .filter(([e]) => e)
    .map(
      ([e, label]) =>
        `<div><strong class="${excludesOne(e) ? "" : "study-null-result"}">${percent(e)}</strong><span>${label}</span><small>IC 95% ${f3(e.lower)}–${f3(e.upper)}</small></div>`,
    )
    .join("");
  const generated = new Date(study.generated_at).toLocaleDateString("pt-BR", {
    timeZone: "UTC",
  });
  document.getElementById("study-scope").textContent =
    `${study.regions} regiões de saúde, internações do SIH/SUS de 2012 a 2025, PM2,5 do ${study.exposure_source === "CAMS" ? "Copernicus/CAMS" : "MERRA-2"}. Risco relativo (RR) de passar de ${fmt(study.contrast_ug_m3[0])} para ${fmt(study.contrast_ug_m3[1])} µg/m³, somando o próprio dia e os ${Number(study.max_lag)} seguintes. Curva exposição-resposta linear por partes com nós em ${study.knots_ug_m3.map(fmt).join(" e ")} µg/m³.`;
  document.getElementById("study-body").innerHTML =
    `<div class="study-headline">${headline}</div>
    <h2>Risco relativo por desfecho e idade</h2>
    <p>Risco relativo (RR) combinado com intervalo de 95%. À direita da linha 1, mais internações com mais PM2,5. A meteorologia do modelo vem do ERA5 (quadrado) ou do MERRA-2 (círculo); o PM2,5 é sempre do CAMS. As duas fontes levam ao mesmo resultado.</p>
    <p class="study-legend"><span><i class="study-key study-key-era5"></i>ERA5</span><span><i class="study-key study-key-merra2"></i>MERRA-2</span></p>
    <div class="study-scroll" tabindex="0" role="region" aria-label="Gráfico de RR por desfecho">${forest(rows, step)}</div>
    <h2>O resultado resiste às sensibilidades</h2>
    <p>Mesmo efeito com erro padrão robusto à autocorrelação (Newey-West, 7 defasagens) e com ajuste pelas internações do dia anterior. I² mede a heterogeneidade entre regiões. Em verde, intervalos que não incluem 1.</p>
    <div class="study-scroll" tabindex="0" role="region" aria-label="Tabela de sensibilidades"><table class="study-table"><caption class="sr-only">Sensibilidades do RR combinado, meteorologia ERA5</caption><thead><tr><th scope="col">Desfecho</th><th scope="col">Regiões</th><th scope="col">RR (IC 95%)</th><th scope="col">Erro robusto (HAC)</th><th scope="col">Desfecho defasado</th><th scope="col">I²</th></tr></thead><tbody>${sensitivity(rows)}</tbody></table></div>
    <h2>Por que o modelo mudou</h2>
    <p>Na versão 6, o modelo só via o clima do próprio dia. Lesões de idosos, que não deveriam responder à poluição, saíam com RR abaixo de 1, como se o PM2,5 protegesse. Na versão 7 entram as médias de temperatura dos dias 1–7 e 8–21 anteriores e de umidade dos dias 1–7: o controle negativo voltou ao nulo e o efeito respiratório cresceu.</p>
    <div class="study-shift">${shift(rows)}</div>
    <p class="study-muted">Versão 6 em cinza, versão 7 em laranja; ambas com meteorologia do MERRA-2, no mesmo painel de ${study.regions} regiões.</p>
    <h2>Limites</h2>
    <ul>
      <li>Resultado de pesquisa: as estimativas não passaram por validação externa e não servem para decisão clínica ou regulatória.</li>
      <li>As ${study.regions} regiões são um subconjunto das regiões de saúde do país; o resultado não vale automaticamente para as demais.</li>
      <li>O PM2,5 vem de reanálise ponderada pela população, não de estação de medição. Erro de exposição tende a puxar o RR para 1.</li>
      <li>Há autocorrelação residual em cerca de metade dos estratos regionais, mas pequena; com erro robusto ou desfecho defasado, o RR combinado muda no máximo ${f3(maxShift)}.</li>
      <li>Asma em 60+ e circulatórias em 0–4 anos têm poucas internações; muitos estratos regionais foram recusados pelas regras de estabilidade.</li>
    </ul>
    <p class="study-muted">Meta-análise gerada em ${generated}, código dos estudos regionais e das meta-análises ${commits}; ${clean} <a href="${import.meta.env.BASE_URL}data/study.json" download>Baixar os resultados (JSON) →</a> · <a href="https://github.com/civisbr/folego/blob/main/docs/methodology.md">Metodologia completa ↗</a></p>`;
}
