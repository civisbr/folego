import "./style.css";
import * as maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";
maplibregl.setWorkerUrl(workerUrl);

const $ = (id) => document.getElementById(id);
const number = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("pt-BR", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const money = (value) => `R$ ${compact.format(value)}`;
const normalize = (text) =>
  text
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
const escape = (value) =>
  String(value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const colors = {
  normal: "#a8c78b",
  attention: "#fac34d",
  alert: "#f28a35",
  emergency: "#cf4a1b",
};
const levels = {
  normal: "Normal",
  attention: "Atenção",
  alert: "Alerta",
  emergency: "Emergência",
};
const stateNames = {
  AC: "Acre",
  AL: "Alagoas",
  AP: "Amapá",
  AM: "Amazonas",
  BA: "Bahia",
  CE: "Ceará",
  DF: "Distrito Federal",
  ES: "Espírito Santo",
  GO: "Goiás",
  MA: "Maranhão",
  MT: "Mato Grosso",
  MS: "Mato Grosso do Sul",
  MG: "Minas Gerais",
  PA: "Pará",
  PB: "Paraíba",
  PR: "Paraná",
  PE: "Pernambuco",
  PI: "Piauí",
  RJ: "Rio de Janeiro",
  RN: "Rio Grande do Norte",
  RS: "Rio Grande do Sul",
  RO: "Rondônia",
  RR: "Roraima",
  SC: "Santa Catarina",
  SP: "São Paulo",
  SE: "Sergipe",
  TO: "Tocantins",
};
const svg = (body) =>
  `<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
$("search-icon").innerHTML = svg(
  '<circle cx="20" cy="20" r="13"/><path d="m30 30 11 11"/>',
);
$("lungs-icon").innerHTML = svg(
  '<path d="M21 5v19l-7 6m13-25v19l7 6M17 15c-4-6-10 0-13 14-3 12 4 16 13 8 3-3 3-6 3-11V17m11-2c4-6 10 0 13 14 3 12-4 16-13 8-3-3-3-6-3-11V17"/>',
);
$("coins-icon").innerHTML = svg(
  '<ellipse cx="17" cy="28" rx="11" ry="5"/><path d="M6 28v13c0 7 22 7 22 0V28M6 35c0 7 22 7 22 0"/><ellipse cx="31" cy="9" rx="11" ry="5"/><path d="M20 9v12m22-12v24c0 5-8 7-14 5M20 17c0 7 22 7 22 0m-14 12c6 0 14-1 14-5"/>',
);
$("alert-icon").innerHTML = svg(
  '<path d="m21 7-17 30c-2 4 0 6 4 6h32c4 0 6-2 4-6L27 7c-2-4-4-4-6 0Z"/><path d="M24 17v12m0 6v1"/>',
);

let geography, forecast, municipalities, map;
let selectedCode = "11004";
let mode = "health";
let filtered = [];
let aggregates = new Map();
let mapReady = false;
let loadFailed = false;
const regionMap = new Map();
const filters = () => ({
  state: $("state").value,
  age: $("age").value,
  horizon: Number($("horizon").value),
});
const level = (relative) =>
  relative < 0.05
    ? "normal"
    : relative < 0.1
      ? "attention"
      : relative < 0.2
        ? "alert"
        : "emergency";
const total = (rows, key) => rows.reduce((sum, row) => sum + row[key], 0);
const dataURL = (path) => `${import.meta.env.BASE_URL}data/${path}`;

async function getJSON(path) {
  const response = await fetch(dataURL(path));
  if (!response.ok)
    throw new Error(`É impossível carregar ${path} (${response.status}).`);
  return response.json();
}

function update() {
  if (!forecast) return;
  const f = filters();
  filtered = geography.features.filter(
    (feature) => !f.state || feature.properties.state === f.state,
  );
  const allowed = new Set(filtered.map((feature) => feature.properties.code));
  const rows = forecast.rows.filter(
    (row) =>
      allowed.has(row.health_region_code) &&
      (!f.age || row.age_group === f.age) &&
      row.day < f.horizon,
  );
  aggregates = new Map(
    filtered.map((feature) => [
      feature.properties.code,
      {
        excess: 0,
        lower: 0,
        upper: 0,
        payment: 0,
        baseline: 0,
        pm25: 0,
        count: 0,
      },
    ]),
  );
  for (const row of rows) {
    const a = aggregates.get(row.health_region_code);
    for (const key of [
      "excess",
      "lower",
      "upper",
      "payment",
      "baseline",
      "pm25",
    ])
      a[key] += row[key];
    a.count++;
  }
  for (const a of aggregates.values()) {
    a.pm25 /= a.count || 1;
    a.level = level(a.excess / (a.baseline || 1));
  }
  $("total-excess").textContent = `+${number.format(total(rows, "excess"))}`;
  $("total-payment").textContent = money(total(rows, "payment"));
  $("total-interval").textContent =
    `Faixa ilustrativa: ${number.format(total(rows, "lower"))}–${number.format(total(rows, "upper"))}`;
  $("total-alerts").textContent = number.format(
    [...aggregates.values()].filter((a) =>
      ["alert", "emergency"].includes(a.level),
    ).length,
  );
  $("total-regions").textContent =
    `de ${number.format(filtered.length)} regiões na demonstração`;
  const start = new Date(`${forecast.forecast_start}T12:00:00`);
  const end = new Date(start);
  end.setDate(end.getDate() + f.horizon - 1);
  const dateFormat = new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "short",
  });
  $("snapshot-label").textContent =
    `${f.state ? stateNames[f.state] : "Brasil"} · ${dateFormat.format(start)} a ${dateFormat.format(end)} de ${start.getFullYear()} · cenário sintético fixo`;
  $("map-date").textContent =
    `${f.horizon} ${f.horizon === 1 ? "dia" : "dias"} de antecedência`;
  $("reset").hidden =
    !f.state && !f.age && f.horizon === 5 && !$("search").value;
  if (!allowed.has(selectedCode)) selectedCode = filtered[0]?.properties.code;
  updateDetail();
  updateMap();
  updateTable();
}

function updateDetail() {
  const feature = regionMap.get(selectedCode);
  const a = aggregates.get(selectedCode);
  if (!feature || !a) return;
  const p = feature.properties;
  const capital = municipalities.find(
    (city) => city.health_region_code === selectedCode && city.is_capital,
  );
  $("region-title").textContent = capital
    ? `${capital.municipality} · ${p.state}`
    : `${p.name} · ${p.state}`;
  $("region-subtitle").textContent = `Região de saúde ${p.name}`;
  $("region-level").textContent = levels[a.level];
  $("region-level").className = `alert-badge ${a.level}`;
  $("region-excess").textContent = `+${number.format(a.excess)}`;
  $("region-payment").textContent = money(a.payment);
  $("region-interval").textContent =
    `Faixa: ${number.format(a.lower)}–${number.format(a.upper)}`;
  $("region-pm25").textContent =
    `PM2,5 médio: ${a.pm25.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} µg/m³`;
  const f = filters();
  $("chart-horizon").textContent =
    `(próximo${f.horizon > 1 ? "s" : ""} ${f.horizon} dia${f.horizon > 1 ? "s" : ""})`;
  const days = Array.from({ length: f.horizon }, (_, day) => {
    const rows = forecast.rows.filter(
      (r) =>
        r.health_region_code === selectedCode &&
        r.day === day &&
        (!f.age || r.age_group === f.age),
    );
    return {
      day,
      value: total(rows, "excess"),
      lower: total(rows, "lower"),
      upper: total(rows, "upper"),
    };
  });
  drawChart(days);
}

function drawChart(days) {
  const w = 420,
    h = 175,
    left = 35,
    right = 12,
    top = 12,
    bottom = 30;
  const max =
    Math.ceil((Math.max(...days.map((d) => d.upper)) * 1.12) / 10) * 10 || 10;
  const x = (i) =>
    days.length === 1
      ? (w + left - right) / 2
      : left + (i * (w - left - right)) / (days.length - 1);
  const y = (v) => top + (h - top - bottom) * (1 - v / max);
  const line = days
    .map((d, i) => `${i ? "L" : "M"}${x(i)},${y(d.value)}`)
    .join(" ");
  const band =
    days.map((d, i) => `${i ? "L" : "M"}${x(i)},${y(d.upper)}`).join(" ") +
    " " +
    days
      .map((d, i) => `L${x(i)},${y(d.lower)}`)
      .reverse()
      .join(" ") +
    " Z";
  const rows = Array.from({ length: 5 }, (_, i) => {
    const value = (max * i) / 4;
    return `<line x1="${left}" x2="${w - right}" y1="${y(value)}" y2="${y(value)}" stroke="#d7d9cb" stroke-dasharray="2 4"/><text x="${left - 8}" y="${y(value) + 4}" text-anchor="end" font-size="10" fill="#5c6255">${number.format(value)}</text>`;
  }).join("");
  $("chart").innerHTML =
    `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Estimativas ilustrativas diárias de internações adicionais: ${days.map((d) => `${d.day + 1}º dia: ${number.format(d.value)}`).join("; ")}"><title>Internações adicionais por dia — demonstração</title>${rows}<path d="${band}" fill="#dbe4ca" opacity=".8"/><path d="${line}" stroke="#173f28" stroke-width="2" fill="none"/>${days.map((d, i) => `<line x1="${x(i)}" x2="${x(i)}" y1="${h - bottom}" y2="${h - bottom + 5}" stroke="#7e8775"/><circle cx="${x(i)}" cy="${y(d.value)}" r="4" fill="#faf8f4" stroke="#173f28" stroke-width="1.7"/><text x="${x(i)}" y="${h - 9}" text-anchor="middle" font-size="11" fill="#414c3a">+${d.day + 1} dia${d.day ? "s" : ""}</text>`).join("")}</svg>`;
}

function updateMap() {
  if (!mapReady) return;
  const f = filters();
  const expression = ["match", ["get", "code"]];
  for (const [code, a] of aggregates)
    expression.push(
      code,
      mode === "health"
        ? colors[a.level]
        : a.pm25 < 15
          ? "#c3d6ad"
          : a.pm25 < 35
            ? "#f6ce64"
            : a.pm25 < 60
              ? "#ef9244"
              : "#c95827",
    );
  expression.push("#e8e8df");
  map.setPaintProperty("regions-fill", "fill-color", expression);
  map.setFilter(
    "regions-fill",
    f.state ? ["==", ["get", "state"], f.state] : null,
  );
  map.setFilter(
    "regions-line",
    f.state ? ["==", ["get", "state"], f.state] : null,
  );
  map.setFilter("selected-line", ["==", ["get", "code"], selectedCode || ""]);
  $("map-legend").innerHTML =
    mode === "health"
      ? Object.entries(levels)
          .map(([key, label]) => `<span><i class="${key}"></i>${label}</span>`)
          .join("")
      : '<span><i class="normal"></i>&lt; 15</span><span><i class="attention"></i>15–35</span><span><i class="alert"></i>35–60</span><span><i class="emergency"></i>≥ 60 µg/m³</span>';
}

function createMap() {
  const protocol = new Protocol();
  maplibregl.addProtocol("pmtiles", protocol.tile);
  map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {},
      layers: [
        {
          id: "background",
          type: "background",
          paint: { "background-color": "#eeeee5" },
        },
      ],
    },
    center: [-53.5, -14],
    zoom: 2.6,
    minZoom: 1.5,
    maxZoom: 10,
    attributionControl: false,
    renderWorldCopies: false,
    dragRotate: false,
    pitchWithRotate: false,
  });
  map.addControl(
    new maplibregl.NavigationControl({ showCompass: false }),
    "top-left",
  );
  map.addControl(
    new maplibregl.AttributionControl({
      compact: true,
      customAttribution: "IBGE · Ministério da Saúde",
    }),
    "bottom-right",
  );
  map.on("load", () => {
    map.addSource("regions", { type: "geojson", data: geography });
    map.addLayer({
      id: "regions-fill",
      type: "fill",
      source: "regions",
      paint: { "fill-color": "#a8c78b", "fill-opacity": 0.94 },
    });
    map.addLayer({
      id: "regions-line",
      type: "line",
      source: "regions",
      paint: { "line-color": "#faf8f4", "line-width": 0.7 },
    });
    map.addLayer({
      id: "selected-line",
      type: "line",
      source: "regions",
      paint: { "line-color": "#173f28", "line-width": 2 },
    });
    for (const [name, coords] of [
      ["Porto Velho", [-63.9, -8.76]],
      ["Manaus", [-60.02, -3.1]],
      ["Belém", [-48.5, -1.45]],
      ["Salvador", [-38.51, -12.97]],
      ["Brasília", [-47.88, -15.79]],
      ["São Paulo", [-46.63, -23.55]],
      ["Rio de Janeiro", [-43.17, -22.91]],
    ]) {
      const marker = document.createElement("span");
      marker.className = "city-marker";
      marker.textContent = name;
      new maplibregl.Marker({ element: marker, anchor: "left" })
        .setLngLat(coords)
        .addTo(map);
    }
    map.fitBounds(
      [
        [-74, -34],
        [-34, 6],
      ],
      { padding: { top: 8, bottom: 16, left: 15, right: 15 }, duration: 0 },
    );
    mapReady = true;
    updateMap();
    map.once("idle", () => {
      $("map").dataset.ready =
        map.queryRenderedFeatures({ layers: ["regions-fill"] }).length > 0
          ? "true"
          : "false";
    });
  });
  map.on("click", "regions-fill", (event) => {
    selectRegion(event.features[0].properties.code);
  });
  map.on("mouseenter", "regions-fill", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "regions-fill", () => {
    map.getCanvas().style.cursor = "";
  });
  map.on("error", (event) => {
    console.warn("Mapa:", event.error?.message);
  });
}

function selectRegion(code, fly = false) {
  const feature = regionMap.get(code);
  if (!feature) return;
  selectedCode = code;
  if ($("state").value && $("state").value !== feature.properties.state)
    $("state").value = "";
  update();
  if (fly && mapReady)
    map.flyTo({
      center: feature.properties.center,
      zoom: 5,
      duration: window.matchMedia("(prefers-reduced-motion: reduce)").matches
        ? 0
        : 650,
    });
}

function updateTable() {
  if ($("region-list").hidden) return;
  $("regions-body").innerHTML = filtered
    .map((feature) => {
      const p = feature.properties,
        a = aggregates.get(p.code);
      return `<tr><td><button data-region="${p.code}">${escape(p.name)} · ${p.state}</button></td><td>${levels[a.level]}</td><td>+${number.format(a.excess)}</td><td>${number.format(a.payment)}</td></tr>`;
    })
    .join("");
}

function search() {
  const query = normalize($("search").value.trim());
  $("search-results").hidden = query.length < 2;

  if (query.length < 2) return;
  const f = filters();
  const cities = municipalities
    .filter(
      (city) =>
        (!f.state || city.state === f.state) &&
        normalize(city.municipality).includes(query),
    )
    .slice(0, 6);
  const regions = geography.features
    .filter(
      (feature) =>
        (!f.state || feature.properties.state === f.state) &&
        normalize(feature.properties.name).includes(query),
    )
    .slice(0, 4);
  $("search-results").innerHTML =
    [
      ...cities.map(
        (city) =>
          `<button data-region="${city.health_region_code}">${escape(city.municipality)} · ${city.state}<small>Município · ${escape(regionMap.get(city.health_region_code)?.properties.name || "")}</small></button>`,
      ),
      ...regions.map(
        (feature) =>
          `<button data-region="${feature.properties.code}">${escape(feature.properties.name)} · ${feature.properties.state}<small>Região de saúde</small></button>`,
      ),
    ].join("") ||
    "<p>Nenhum resultado. Tente outro município ou limpe o filtro de estado.</p>";
}

function openDetails() {
  const p = regionMap.get(selectedCode)?.properties,
    a = aggregates.get(selectedCode);
  if (!p || !a) return;
  const cities = municipalities.filter(
    (city) => city.health_region_code === selectedCode,
  );
  $("dialog-title").textContent = `${p.name} · ${p.state}`;
  $("dialog-content").innerHTML =
    `<p>Região de saúde <b>${escape(p.code)}</b> · ${cities.length} municípios na correspondência territorial.</p><p><b>Período selecionado:</b> ${filters().horizon} dias. <b>Internações adicionais:</b> +${number.format(a.excess)}. <b>Pagamento adicional:</b> ${money(a.payment)}.</p><p>Valores sintéticos; a faixa exibida é ilustrativa, sem interpretação probabilística. Os níveis de alerta usam limiares provisórios de excesso relativo: 5%, 10% e 20%.</p><h3 style="margin-top:22px">Municípios desta região</h3><p style="margin-top:12px">${cities.map((city) => escape(city.municipality)).join(" · ")}</p><p style="margin-top:22px"><a href="#dados" id="dialog-data">Acessar arquivos abertos →</a></p>`;
  $("region-dialog").showModal();
  $("dialog-data").addEventListener("click", () => $("region-dialog").close());
}

const pages = {
  "como-funciona": () =>
    `<div class="eyebrow">METODOLOGIA E LIMITAÇÕES</div><h1>Da fumaça ao planejamento.</h1><p>O Fôlego foi desenhado para estimar quantas internações respiratórias adicionais podem ocorrer após a exposição à fumaça de queimadas — e quanto isso pode representar em valores pagos pelo SUS.</p><div class="content-callout"><b>Estado atual: demonstração.</b><p>A interface usa números sintéticos. A implementação estatística passou por testes com dados artificiais de efeito conhecido; ainda não há modelo nacional treinado nem validação histórica publicada.</p></div><h2>Como a estimativa será produzida</h2><ol><li><b>Histórico de saúde.</b> Internações do SIH/SUS com diagnóstico J00–J99, por residência, data de internação, região de saúde e faixa etária. Asma, pneumonia e causas circulatórias têm análises separadas.</li><li><b>Exposição.</b> PM2,5 do Copernicus/CAMS em µg/m³, ponderado pela população dos pixels de cada região. Os focos do NASA FIRMS descrevem o fogo; não substituem a medição de exposição à fumaça.</li><li><b>Modelo.</b> Regressão quasi-Poisson com base cruzada de exposição e defasagens de 0–7 dias. Temperatura, umidade, sazonalidade, tendência, dia da semana e feriados entram como confundidores. SRAG entra quando há granularidade compatível.</li><li><b>Contrafactual.</b> Comparamos a previsão com um cenário de referência de 15 µg/m³. O histórico observado é preservado; alteramos a exposição dos dias futuros. A diferença é o excesso atribuível estimado.</li><li><b>Pagamento.</b> O excesso é multiplicado pelo valor médio de AIH da mesma região, idade e diagnóstico, corrigido pelo IPCA. <b>Esse valor é a remuneração pelo SUS, não o custo real do hospital.</b></li></ol><h2>Incerteza e alertas</h2><p>Na implementação do modelo, a faixa de 95% usa sorteios conjuntos dos coeficientes e preserva a covariância entre dias e cenários. É um intervalo da média condicional; não inclui toda a incerteza de contagem futura, de exposição ou de especificação. No painel de demonstração, as faixas são apenas ilustrações.</p><p>Os limiares provisórios de excesso relativo são: normal &lt; 5%; atenção 5–10%; alerta 10–20%; emergência ≥ 20%. Não são limiares oficiais de vigilância ou recomendações clínicas. A camada PM2,5 tem escala própria, em µg/m³.</p><h2>O que precisa ser validado</h2><p>Backtest com origem móvel entre 2019 e 2024, MAE e MAPE semanais por região, precisão e recall de picos, controle negativo com S00–T98 e sensibilidade CAMS versus MERRA-2. O SIH tem atraso de publicação; cada rodada histórica precisa respeitar quais dados estavam disponíveis na data da previsão.</p><h2>Limitações</h2><ul><li>Dados ecológicos sustentam associações populacionais; a inferência causal é limitada pelo desenho do estudo.</li><li>O SIH cobre internações financiadas pelo SUS, com possíveis erros de CID e sub-registro; não inclui toda a saúde suplementar.</li><li>PM2,5 total não identifica sozinho a fração proveniente de queimadas. Atribuir todo o efeito à fumaça exige separar outras fontes e fazer análises de sensibilidade.</li><li>Satélites e reanálises têm erro de exposição; umidade, temperatura e vírus sazonais podem deixar confundimento residual.</li><li>O SIH não informa o número de leitos disponíveis. Internações adicionais não equivalem diretamente a ocupação de leitos.</li></ul><h2>Referências e reprodutibilidade</h2><p>O desenho segue a especificação do projeto e o método de <a href="https://www.jstatsoft.org/article/view/v043i08">Gasparrini (2011)</a>. A base cruzada em Python usa funções lineares por partes para exposição e polinômios de Legendre para defasagens; a equivalência com o pacote R dlnm ainda precisa ser avaliada.</p><p>Consulte <a href="https://github.com/civisbr/folego/blob/main/PROJECT.md">os requisitos completos</a>, o <a href="https://github.com/civisbr/folego/blob/main/docs/validation.md">relatório de validação</a> e o código-fonte para reproduzir os testes.</p>`,
  dados: () =>
    `<div class="eyebrow">DADOS ABERTOS · CC BY 4.0</div><h1>Transparência para respirar melhor.</h1><p>Arquivos estáticos, documentados e acessíveis sem cadastro. A malha e a correspondência territorial vêm de fontes públicas oficiais. Os valores de saúde deste protótipo são sintéticos.</p><div class="data-cards"><article class="data-card"><h2>Cenário de demonstração</h2><p>Estimativas ilustrativas por região de saúde, idade e dia. Inclui a marcação <code>mode: demonstration</code>.</p><a href="${dataURL("forecast.json")}" download>Baixar JSON →</a></article><article class="data-card"><h2>Regiões de saúde</h2><p>Polígonos municipais do IBGE agregados pela correspondência do Ministério da Saúde. Cobertura e versões no manifesto.</p><a href="${dataURL("regions.geojson")}" download>Baixar GeoJSON →</a></article><article class="data-card"><h2>Correspondência territorial</h2><p>Códigos IBGE de 7 dígitos, DATASUS de 6 e município → região de saúde. Busca nacional incluída.</p><a href="${dataURL("municipalities.json")}" download>Baixar JSON →</a> · <a href="${dataURL("municipalities.csv")}" download>CSV →</a></article><article class="data-card"><h2>Dados do recorte atual</h2><p>Baixe as linhas sintéticas que correspondem ao estado, faixa etária e horizonte selecionados no painel.</p><button id="download-filtered" class="text-button">Exportar recorte em CSV →</button></article></div><h2>API pública estática</h2><p>Os mesmos arquivos alimentam o painel. Não há autenticação nem backend. A URL é relativa à raiz do site, inclusive no GitHub Pages:</p><p><code>GET ./data/forecast.json</code><br><code>GET ./data/regions.geojson</code><br><code>GET ./data/municipalities.json</code><br><code>GET ./data/manifest.json</code></p><p>Para filtrar, leia o JSON e selecione <code>health_region_code</code>, <code>age_group</code> e <code>day</code> (0–4). O código regional é uma string. O esquema completo está em <a href="${dataURL("schema.json")}">schema.json</a>.</p><h2>Privacidade antes de publicar</h2><p>Microdados e identificadores de AIH nunca são exportados. Contagens observadas menores que 5 e seus valores financeiros são suprimidos. Publicamos apenas um nível de agregação, sem marginais que permitam recuperar células por subtração. Dados sintéticos do painel não correspondem a pacientes.</p><h2>Fontes e licenças</h2><p>Atribuição: DATASUS/Ministério da Saúde, Copernicus/CAMS, NASA FIRMS e IBGE. Código sob MIT. Dados derivados sob CC BY 4.0, preservadas as condições das fontes. <a href="${dataURL("manifest.json")}">Consulte o manifesto de proveniência →</a></p>`,
  sobre: () =>
    `<div class="eyebrow">UMA INICIATIVA CIVISBR</div><h1>Mais antecedência.<br>Mais fôlego para o SUS.</h1><p>Quando a fumaça chega a uma cidade, ela já viajou muito. Seu impacto na saúde pode continuar aparecendo nos dias seguintes. O Fôlego nasce para ajudar a transformar esse intervalo em tempo para o planejamento.</p><h2>Uma pergunta de interesse público</h2><p>Quanto a fumaça das queimadas pode representar em internações e valores pagos pelo SUS nos próximos dias, em cada região de saúde do Brasil?</p><p>Queremos responder a essa pergunta em escala nacional, de forma contínua e aberta. Secretarias de saúde, equipes de vigilância, pesquisadores e a sociedade poderão consultar os resultados, entender as limitações e reproduzir os cálculos.</p><div class="content-callout"><b>Não é diagnóstico individual.</b><p>O Fôlego não é uma ferramenta clínica e não substitui a vigilância epidemiológica oficial. Não use os dados de demonstração para decisões operacionais.</p></div><h2>Construção pública</h2><p>A primeira versão entrega a interface, a correspondência territorial e a base de ingestão, transformação, modelagem e avaliação. A integração histórica nacional e a validação científica são etapas abertas e documentadas.</p><p><a class="button primary" href="https://github.com/civisbr/folego">Conheça o projeto no GitHub ↗</a></p><h2>Contribua</h2><p>Encontrou um problema nos dados, quer revisar a metodologia ou melhorar a acessibilidade? <a href="https://github.com/civisbr/folego/issues">Abra uma issue</a>. Contribuições científicas e técnicas são bem-vindas; consulte o guia de contribuição no repositório.</p>`,
};

function route() {
  const key = location.hash.slice(1) || "painel";
  const isPanel = !pages[key];
  $("dashboard").hidden = !isPanel;
  $("content-page").hidden = isPanel;
  document.querySelectorAll("nav a").forEach((a) => {
    if (a.hash === `#${isPanel ? "painel" : key}`)
      a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.title = `${isPanel ? "Painel" : key === "dados" ? "Dados abertos" : key === "sobre" ? "Sobre" : "Como funciona"} · Fôlego`;
  if (!isPanel) {
    $("content-page").innerHTML = pages[key]();
    $("download-filtered")?.addEventListener("click", downloadFiltered);
  } else if (map) requestAnimationFrame(() => map.resize());
  if (loadFailed) $("error").hidden = false;
}

function downloadFiltered() {
  if (!forecast) return;
  const f = filters(),
    allowed = new Set(filtered.map((feature) => feature.properties.code));
  const rows = forecast.rows.filter(
    (r) =>
      allowed.has(r.health_region_code) &&
      (!f.age || r.age_group === f.age) &&
      r.day < f.horizon,
  );
  const keys = [
    "health_region_code",
    "age_group",
    "day",
    "excess",
    "lower",
    "upper",
    "payment",
    "pm25",
  ];
  const csv = [
    "mode," + keys.join(","),
    ...rows.map((r) => "demonstration," + keys.map((k) => r[k]).join(",")),
  ].join("\r\n");
  const url = URL.createObjectURL(
    new Blob(["\ufeff", csv], { type: "text/csv;charset=utf-8" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = "folego-demonstracao.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

$("state").addEventListener("change", () => {
  update();
  if (mapReady) {
    const f = filters();
    if (f.state) {
      const city = filtered[0];
      if (city)
        map.flyTo({ center: city.properties.center, zoom: 4, duration: 0 });
    } else
      map.fitBounds(
        [
          [-74, -34],
          [-34, 6],
        ],
        { padding: 20, duration: 0 },
      );
  }
});
for (const id of ["age", "horizon"]) $(id).addEventListener("change", update);
$("search").addEventListener("input", search);
$("search").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    $("search-results").hidden = true;
  }
  if (event.key === "ArrowDown") {
    event.preventDefault();
    $("search-results").querySelector("button")?.focus();
  }
});
$("search-results").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    $("search-results").hidden = true;
    $("search").focus();
  }
});
$("search-results").addEventListener("click", (event) => {
  const button = event.target.closest("[data-region]");
  if (!button) return;
  selectRegion(button.dataset.region, true);
  $("search").value = button.firstChild.textContent;
  $("search-results").hidden = true;
  $("reset").hidden = false;
  $("search").focus();
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".search-field")) {
    $("search-results").hidden = true;
  }
});
$("reset").addEventListener("click", () => {
  $("state").value = "";
  $("age").value = "";
  $("horizon").value = "5";
  $("search").value = "";
  selectedCode = "11004";
  update();
  if (mapReady)
    map.fitBounds(
      [
        [-74, -34],
        [-34, 6],
      ],
      { padding: 20, duration: 0 },
    );
});
for (const [id, newMode] of [
  ["health-layer", "health"],
  ["pm-layer", "pm"],
])
  $(id).addEventListener("click", () => {
    mode = newMode;
    for (const [buttonId, active] of [
      ["health-layer", mode === "health"],
      ["pm-layer", mode === "pm"],
    ]) {
      $(buttonId).classList.toggle("active", active);
      $(buttonId).setAttribute("aria-pressed", String(active));
    }
    updateMap();
  });
$("show-table").addEventListener("click", () => {
  $("region-list").hidden = false;
  updateTable();
  $("region-list").scrollIntoView({ block: "start" });
});
$("hide-table").addEventListener("click", () => {
  $("region-list").hidden = true;
  $("show-table").focus();
});
$("regions-body").addEventListener("click", (event) => {
  const button = event.target.closest("[data-region]");
  if (button) {
    selectRegion(button.dataset.region, true);
    $("region-title").scrollIntoView({ block: "center" });
  }
});
$("region-details").addEventListener("click", openDetails);
$("close-dialog").addEventListener("click", () => $("region-dialog").close());
$("region-dialog").addEventListener("click", (event) => {
  if (event.target === $("region-dialog")) {
    const rect = event.target.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      event.target.close();
  }
});
window.addEventListener("hashchange", () => {
  route();
  window.scrollTo(0, 0);
});
route();

try {
  [geography, forecast, municipalities] = await Promise.all([
    getJSON("regions.geojson"),
    getJSON("forecast.json"),
    getJSON("municipalities.json"),
  ]);
  if (forecast.mode !== "demonstration")
    throw new Error(
      "Esta versão da interface exige um conjunto de demonstração explicitamente identificado.",
    );
  for (const feature of geography.features)
    regionMap.set(feature.properties.code, feature);
  for (const [code, name] of Object.entries(stateNames).sort((a, b) =>
    a[1].localeCompare(b[1], "pt-BR"),
  )) {
    const option = document.createElement("option");
    option.value = code;
    option.textContent = `${name} · ${code}`;
    $("state").append(option);
  }
  update();
  try {
    createMap();
  } catch (error) {
    $("map").innerHTML =
      '<p style="padding:24px">O mapa exige WebGL. Use “Ver lista de regiões” abaixo para explorar todos os dados.</p>';
    console.warn(error.message);
  }
} catch (error) {
  loadFailed = true;
  $("error").hidden = false;
  $("error").textContent =
    `Não foi possível carregar os dados do painel. Recarregue a página ou tente novamente mais tarde. ${error.message}`;
  $("snapshot-label").textContent = "Dados indisponíveis";
}
