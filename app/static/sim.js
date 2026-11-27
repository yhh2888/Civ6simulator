/* Civ VI City Studio — simulation UI.
 *
 * Talks to the /api/sim, /api/scenarios, /api/validation and /api/data
 * endpoints. Integrates with the existing editor script (index.html) through
 * its globals (state, $, drawMap, selectCity, renderCities, setStatus, ...).
 * All calculations happen on the server; this file only renders results.
 */
(function () {
  "use strict";

  const YK = ["Food", "Production", "Gold", "Science", "Culture", "Faith"];
  const YL = { Food: "식량", Production: "생산력", Gold: "금", Science: "과학", Culture: "문화", Faith: "신앙" };
  const YCOL = { Food: "#7ac36a", Production: "#d08b4c", Gold: "#e3c04f", Science: "#5fa8e8", Culture: "#c07ad6", Faith: "#d9d2b8" };
  // Categorical palette (validated: dark mode vs #131f29, all checks pass).
  const SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c"];
  const STATUS_CLASS = { "검증 완료": "s-verified", "부분 구현": "s-partial", "미검증": "s-unverified", "미지원": "s-unsupported" };
  const CATEGORY_LABEL = {
    tiles: "작업 타일", specialists: "전문가", population: "인구 기본 산출", districts: "특수지구 인접",
    buildings: "건물·불가사의", trade_routes: "교역로", great_works: "걸작", build_bonus: "생산 항목 보너스", modifiers: "정책·특성 Modifier", custom: "사용자 정의", projects: "프로젝트 전환", percent: "% 보정",
  };

  const S = {
    scenarioId: null, state: null, detail: null, overlay: "details", citizenEdit: false,
    presets: [], scenarios: [], empire: null, prod: null, prodFilter: { cat: "", onlyAvailable: true },
    log: [], history: [], previewHistory: null, compare: null, compareSel: new Set(), lastReport: null,
    optimize: null, empireResult: null, coverage: null, audit: null, modAudit: null, validation: null,
    ledger: null, ledgerYield: "all", ledgerExcluded: true,
    yieldLedger: null, yieldLedgerKey: "Gold", yieldLedgerOpen: new Set(), yieldLedgerStale: true,
    chartMetric: "Production", chartSource: "history", tiles: new Map(), suppress: 0, timer: null, busy: false,
  };

  // ------------------------------------------------------------- helpers
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // Civ VI shows yields cut down (not rounded) to one decimal: 3.29 → 3.2, -2.55 → -2.5.
  const trunc = (v, d) => { const f = 10 ** d; return Math.trunc(Number(v) * f + (Number(v) >= 0 ? 1e-9 : -1e-9)) / f; };
  const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "–" : trunc(v, d).toLocaleString("ko-KR", { maximumFractionDigits: d }));
  const sgn = (v, d = 1) => (v === null || v === undefined ? "–" : (v > 0 ? "+" : "") + num(v, d));
  const label = (id) => (typeof codeLabel === "function" ? codeLabel(id) : String(id || ""));
  const cls = (v) => (v > 1e-9 ? "pos" : v < -1e-9 ? "neg" : "");
  const yieldsInline = (y, d = 1) => `<span class="yield-row">${YK.map((k) => `<span class="yv ${k}" title="${YL[k]}">${num(y?.[k] ?? 0, d)}</span>`).join("")}</span>`;
  const badge = (status) => `<span class="badge ${STATUS_CLASS[status] || "s-unverified"}">${esc(status)}</span>`;

  function q(path) {
    if (!S.scenarioId) return path;
    return path + (path.includes("?") ? "&" : "?") + "scenario_id=" + encodeURIComponent(S.scenarioId);
  }
  async function api(path, method = "GET", body = undefined, scoped = true) {
    const response = await fetch(scoped ? q(path) : path, {
      method, cache: "no-store",
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? data.detail.map((d) => d.msg).join(", ") : data.detail;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }
  let legacySetStatus = null;
  function status(msg) {
    S.suppress++;
    try { (legacySetStatus || setStatus)(msg); } finally { S.suppress--; }
  }
  function toast(msg) {
    const t = document.createElement("div");
    t.className = "toast"; t.textContent = msg; document.body.appendChild(t);
    setTimeout(() => t.remove(), 3200);
  }
  async function guarded(fn, okMsg) {
    if (S.busy) return;
    S.busy = true; document.body.classList.add("busy");
    try { const r = await fn(); if (okMsg) status(okMsg); return r; }
    catch (error) { status("오류: " + error.message); toast("오류: " + error.message); }
    finally { S.busy = false; document.body.classList.remove("busy"); }
  }
  const city = () => (typeof selectedCity === "function" ? selectedCity() : null);
  const isCenter = (c, x, y) => c && c.x === x && c.y === y;

  // ---------------------------------------------------------------- tabs
  function setupTabs() {
    document.querySelectorAll("[data-tabs]").forEach((bar) => {
      const root = bar.parentElement;
      const buttons = [...bar.querySelectorAll(".tab")];
      const activate = (key) => {
        buttons.forEach((b) => b.classList.toggle("active", b.dataset.tab === key));
        root.querySelectorAll(":scope > .pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === key));
        try { localStorage.setItem("civ6-tab-" + bar.dataset.tabs, key); } catch (e) { /* ignore */ }
        onTab(key);
      };
      buttons.forEach((b) => (b.onclick = () => activate(b.dataset.tab)));
      let saved = null;
      try { saved = localStorage.getItem("civ6-tab-" + bar.dataset.tabs); } catch (e) { /* ignore */ }
      activate(buttons.some((b) => b.dataset.tab === saved) ? saved : buttons[0].dataset.tab);
    });
  }
  function showTab(group, key) {
    const button = document.querySelector(`[data-tabs="${group}"] .tab[data-tab="${key}"]`);
    if (button) button.click();
  }
  function onTab(key) {
    if (key === "production" && !S.prod) void loadProductionOptions();
    if (key === "empire" && !S.empire) void loadEmpire();
    if (key === "basis") void loadBasis();
    if (key === "data" && !S.audit) void loadAudit();
    if (key === "chart") renderChart();
    if (key === "ledger" && (!S.ledger || S.ledgerCity !== (city() || {}).id)) void loadLedger();
    if (key === "yieldledger" && (S.yieldLedgerStale || !S.yieldLedger)) void loadYieldLedger();
    if (key === "scenarios") renderScenarios();
  }

  // ------------------------------------------------------------ refresh
  function scheduleRefresh() {
    if (S.suppress) return;
    clearTimeout(S.timer);
    S.timer = setTimeout(() => void refreshAll(), 350);
  }
  async function refreshAll() {
    try {
      S.state = await api("/api/sim/state?details=true");
      indexTiles();
      computeOverlayMax();
      renderTop();
      renderCityCards();
      renderEmpireBox();
      drawMap();
      S.yieldLedgerStale = true;
      if (document.querySelector('[data-tabs="bottom"] .tab.active')?.dataset.tab === "yieldledger") void loadYieldLedger();
      await refreshCity();
    } catch (error) {
      status("시뮬레이션 상태를 불러오지 못했습니다: " + error.message);
    }
  }
  function indexTiles() {
    S.tiles = new Map();
    if (!S.state) return;
    for (const [cityId, c] of Object.entries(S.state.cities)) {
      const prefs = (state.cities.find((x) => x.id === cityId) || {}).tile_preferences || {};
      for (const t of c.yields.tiles || []) {
        S.tiles.set(t.key, { ...t, cityId, pref: prefs[t.key] || null });
      }
      for (const d of c.yields.districts || []) {
        const t = S.tiles.get(d.key);
        if (t) t.adjacency = d.adjacency;
      }
    }
  }
  async function refreshCity() {
    const c = city();
    if (!c || !S.state || !S.state.cities[c.id]) {
      S.detail = null;
      renderOverview(); renderCitizens(); renderProduction(); renderTileDetail();
      return;
    }
    // The ledger is fetched together with the city detail (in parallel), so it
    // is ready by the time the ledger tab is opened.
    const ledgerRequest = api(`/api/sim/cities/${encodeURIComponent(c.id)}/ledger`).catch(() => null);
    try {
      S.detail = await api(`/api/sim/cities/${encodeURIComponent(c.id)}`);
    } catch (error) {
      S.detail = null;
      status("도시 상세를 불러오지 못했습니다: " + error.message);
    }
    renderOverview(); renderCitizens(); renderProduction(); renderTileDetail(); renderHistory();
    const bottomTab = document.querySelector('[data-tabs="bottom"] .tab.active')?.dataset.tab;
    if (bottomTab === "basis") void loadBasis();
    const ledger = await ledgerRequest;
    if (city() && city().id === c.id) { S.ledger = ledger; S.ledgerCity = c.id; renderLedger(); }
  }

  // ------------------------------------------------------------ top bar
  function renderTop() {
    const st = S.state; if (!st) return;
    $("simTurn").textContent = st.turn;
    $("simSpeed").textContent = st.game_speed;
    const r = st.empire.research;
    const part = (t, name) => (t.current_id ? `${name} ${label(t.current_id)} ${num(t.progress, 0)}/${num(t.cost, 0)}${t.turns_remaining ? ` (${t.turns_remaining}턴)` : ""}` : `${name} 선택 안 됨`);
    $("simResearch").textContent = `${part(r.technology, "기술")} · ${part(r.civic, "사회")}`;
    $("simResearch").title = $("simResearch").textContent;
    $("simEmpireScience").textContent = num(st.empire.yields.Science);
    $("simEmpireCulture").textContent = num(st.empire.yields.Culture);
    $("simEmpireGold").textContent = `${sgn(st.empire.net_gold)}`;
    $("simEmpireGold").title = `도시 금 ${num(st.empire.yields.Gold)} − 유지비 ${num(st.empire.maintenance.total)} · 국고 ${num(st.empire.treasury?.Gold ?? 0)}`;
    const banner = $("scenarioBanner");
    if (S.scenarioId) {
      const sc = S.scenarios.find((s) => s.id === S.scenarioId);
      banner.classList.remove("hidden");
      banner.innerHTML = `<b>시나리오 모드</b> ${esc(sc?.name || S.scenarioId)} · 턴 ${esc(st.turn)} (시작 ${esc(sc?.base_turn ?? "-")}) — 현재 게임 상태는 변경되지 않습니다.
        <button type="button" id="bannerReset">원본 상태로 복원</button><button type="button" id="bannerExit">현재 게임으로 돌아가기</button>`;
      $("bannerReset").onclick = () => scenarioAction("reset", S.scenarioId);
      $("bannerExit").onclick = () => switchScenario("");
    } else {
      banner.classList.add("hidden");
    }
    document.documentElement.style.setProperty("--topbar-h", (document.querySelector(".topbar").offsetHeight + (S.scenarioId ? banner.offsetHeight : 0)) + "px");
  }

  // --------------------------------------------------------- left panel
  function renderCityCards() {
    const root = $("simCityCards"); if (!root) return;
    if (!state.cities.length) { root.innerHTML = '<p class="muted">아직 도시가 없습니다. 지도에서 타일을 고른 뒤 ‘새 도시’를 누르세요.</p>'; return; }
    root.innerHTML = "";
    for (const c of state.cities) {
      const ev = S.state?.cities?.[c.id];
      const card = document.createElement("div");
      card.className = "city-card" + (c.id === state.selectedCityId ? " active" : "");
      const g = ev?.growth;
      const head = ev?.production?.queue?.[0];
      card.innerHTML = `<div class="name"><span>🏛 ${esc(c.name)}</span><span>인구 ${esc(c.population)}</span></div>
        ${ev ? yieldsInline(ev.yields.totals) : ""}
        <div class="meta">${g ? (g.turns_to_growth ? `성장 ${g.turns_to_growth}턴` : g.effective_food_change < 0 ? "식량 부족" : "성장 정체") + ` · 식량 ${num(g.stored_food)}/${num(g.food_required, 0)}` : ""}</div>
        <div class="meta">${head ? `생산 ${esc(label(head.target_id))} ${head.estimated_turns ? head.estimated_turns + "턴" : ""}` : "생산 대기열 비어 있음"}${ev ? ` · ${ev.citizen_mode === "auto" ? "자동" : "수동"} 배치` : ""}</div>`;
      card.onclick = () => selectCity(c.id);
      if (!S.scenarioId) {
        const del = document.createElement("button");
        del.className = "del"; del.type = "button"; del.textContent = "삭제";
        del.onclick = (e) => { e.stopPropagation(); confirmAndDeleteCity(c.id); };
        card.appendChild(del);
      }
      root.appendChild(card);
    }
  }
  function renderEmpireBox() {
    const st = S.state; if (!st) return;
    const e = st.empire;
    $("simEmpireBox").innerHTML = `<div class="panel-title">제국</div>
      <div>정부 <b>${esc(label(e.government) || "없음")}</b></div>
      <div>정책 ${e.policies.length ? e.policies.map((p) => `<span class="chip">${esc(label(p))}</span>`).join("") : "없음"}</div>
      <div>문명 <b>${esc(label(e.civilization) || "-")}</b> · 지도자 <b>${esc(label(e.leader) || "-")}</b></div>
      <div>국고 금 <b>${num(e.treasury?.Gold ?? 0)}</b> · 신앙 <b>${num(e.treasury?.Faith ?? 0)}</b></div>
      <div class="sub">유지비 ${num(e.maintenance.total)} / 턴 · Modifier 적용 ${st.modifiers.applied_effects} · 미지원 ${st.modifiers.skipped}</div>`;
  }

  // ----------------------------------------------------------- overview
  function renderOverview() {
    const root = $("simOverview"); if (!root) return;
    const d = S.detail;
    if (!d) { root.innerHTML = '<p class="muted">도시를 선택하세요.</p>'; return; }
    const c = d.city, g = c.growth, h = c.housing, a = c.amenities, nt = d.next_turn;
    const pct = g.food_required ? Math.min(100, (g.stored_food / g.food_required) * 100) : 0;
    const rows = [
      ["인구", "population", 0], ["저장 식량", "stored_food", 1], ["식량 산출", "food_yield", 1],
      ["식량 소비", "food_consumption", 1], ["식량 잉여", "food_surplus", 1], ["성장 반영 식량 변화", "effective_food_change", 2],
    ];
    const cur = nt?.current, nxt = nt?.next;
    const tableRows = rows.map(([lbl, key, dd]) => {
      const a1 = cur?.[key], b1 = nxt?.[key];
      const diff = typeof a1 === "number" && typeof b1 === "number" ? b1 - a1 : null;
      return `<tr><td>${lbl}</td><td class="num">${num(a1, dd)}</td><td class="num">${num(b1, dd)}</td><td class="num ${cls(diff)}">${diff === null ? "–" : sgn(diff, dd)}</td></tr>`;
    }).join("") + YK.map((k) => {
      const a1 = cur?.yields?.[k], b1 = nxt?.yields?.[k];
      const diff = (b1 ?? 0) - (a1 ?? 0);
      return `<tr><td><span class="yv ${k}">${YL[k]}</span></td><td class="num">${num(a1, 1)}</td><td class="num">${num(b1, 1)}</td><td class="num ${cls(diff)}">${sgn(diff, 2)}</td></tr>`;
    }).join("") + [["주거공간", "housing", 1], ["쾌적도 점수", "amenity_score", 0], ["성장까지 남은 턴", "turns_to_growth", 0]].map(([lbl, key, dd]) => {
      const a1 = cur?.[key], b1 = nxt?.[key];
      const diff = typeof a1 === "number" && typeof b1 === "number" ? b1 - a1 : null;
      return `<tr><td>${lbl}</td><td class="num">${num(a1, dd)}</td><td class="num">${num(b1, dd)}</td><td class="num ${cls(diff)}">${diff === null ? "–" : sgn(diff, dd)}</td></tr>`;
    }).join("");
    const reasons = (nt?.reasons || []).map((r) => {
      if (r.delta) return `<li>${esc(r.label)}: ${YK.filter((k) => Math.abs(r.delta[k]) > 1e-9).map((k) => `${YL[k]} ${sgn(r.delta[k], 2)}`).join(", ")}</li>`;
      if (r.category === "assignment") return `<li>시민 배치 변경: +${esc((r.added_tiles || []).join(" ")) || "없음"} / −${esc((r.removed_tiles || []).join(" ")) || "없음"}</li>`;
      if (r.category === "population_change") return `<li>인구 ${esc(r.from)} → ${esc(r.to)}</li>`;
      return `<li>${esc(r.label)}</li>`;
    }).join("");
    const events = (nt?.events || []).map((e) => `<li>${esc(describeEvent(e))}</li>`).join("");
    const last = S.lastReport?.cities?.[c.id];
    root.innerHTML = `
      <div class="block"><h4><span>${esc(c.name)} · 인구 ${c.population}</span><span>${esc(c.citizen_mode === "auto" ? "자동 배치" : "수동 배치")} · ${esc(presetLabel(c.strategy))}</span></h4>
        ${yieldsInline(c.yields.totals, 2)}
        <div class="bar"><span style="width:${pct}%"></span></div>
        <div class="kv">
          <span>식량</span><span>${num(g.stored_food)} / ${num(g.food_required, 1)} (${g.turns_to_growth ? g.turns_to_growth + "턴 후 성장" : g.effective_food_change < 0 ? "감소 중" : "성장 없음"})</span>
          <span>잉여 → 성장 반영</span><span>${sgn(g.food_surplus)} → ${sgn(g.effective_food_change, 2)} (수정치 ${sgn(g.growth_modifier_percent, 0)}%, 주거 ×${g.housing_multiplier})</span>
          <span>주거공간</span><span>${num(h.value)} ${h.mode === "manual" ? "(수동)" : "(계산)"} · ${esc(g.housing_reason)}</span>
          <span>쾌적도</span><span>${num(a.supply, 0)} − 요구 ${a.required} = ${sgn(a.score, 0)} · ${esc(label(a.happiness))} (성장 ${sgn(a.growth_modifier, 0)}%, 비식량 ${sgn(a.non_food_yield_modifier, 0)}%)</span>
          <span>시민</span><span>${c.yields.citizens_assigned} / ${c.population} 배치${c.yields.unassigned_citizens ? ` · 미배치 ${c.yields.unassigned_citizens}` : ""}</span>
        </div>
        ${(c.yields.warnings || []).map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
      </div>
      <div class="block"><h4><span>현재 턴 vs 다음 턴 예상</span><span class="sub">턴 ${esc(d.turn)} → ${esc(d.turn + 1)}</span></h4>
        <table class="grid cmp-table"><thead><tr><th>항목</th><th class="num">현재 턴</th><th class="num">다음 턴 예상</th><th class="num">차이</th></tr></thead><tbody>${tableRows}</tbody></table>
        <div class="sub" style="margin-top:6px">변화 원인</div><ul class="reasons">${reasons || "<li>산출 구성 변화 없음</li>"}</ul>
        <div class="sub">이번 턴에 일어날 일</div><ul class="reasons">${events || "<li>특별한 이벤트 없음</li>"}</ul>
        <p class="note">${esc(d.order_note)}</p>
      </div>
      ${last ? `<div class="block"><h4><span>직전 실행 결과 (턴 ${esc(S.lastReport.turn_processed)})</span></h4>
        <div class="kv"><span>인구</span><span>${last.current.population} → ${last.next?.population ?? "-"}</span>
        <span>생산 완료</span><span>${esc(last.events.filter((e) => e.type === "completed").map((e) => label(e.id)).join(", ") || "없음")}</span></div></div>` : ""}
      <div class="block"><h4><span>산출량 계산 근거</span><button type="button" class="mini" id="ovLedger">전체 장부 보기 ↓</button></h4>${renderBreakdown(c.yields)}</div>
      ${renderCitySettings(c)}`;
    bindBreakdown(root);
    bindCitySettings(root, c);
    root.querySelector("#ovLedger").onclick = () => { document.body.classList.remove("dock-collapsed"); showTab("bottom", "ledger"); };
  }
  function describeEvent(e) {
    const name = label(e.id || e.target_id || "");
    switch (e.type) {
      case "growth": return `인구 성장 → ${e.population} (남은 식량 ${num(e.stored_food_after)})`;
      case "completed": return `생산 완료: ${name}${e.population_cost ? ` (인구 −${e.population_cost})` : ""}`;
      case "flat_bonus": return `건설 항목 보너스 생산력 +${num(e.amount, 0)} → ${label(e.to)} (도시국가 사절 등)`;
      case "overflow_carried": return `초과 생산 ${num(e.amount, 1)} → ${label(e.to)}`;
      case "overflow_stored": return `초과 생산 ${num(e.amount, 1)} 보관 (대기열 비어 있음)`;
      case "overflow_discarded": return `초과 생산 ${num(e.amount, 1)} 폐기`;
      case "idle": return e.category ? `${e.category === "technology" ? "기술" : "사회제도"} 연구 미선택 — ${num(e.discarded, 1)} 미사용` : `생산 대기열 비어 있음 — 생산력 ${num(e.unused, 1)} 미사용`;
      case "blocked": return `생산 중단: ${name} (${(e.reasons || []).join("; ")})`;
      case "auto_assignment": return `자동 시민 배치 (${presetLabel(e.strategy)})`;
      case "new_citizen_placed": return `새 시민 배치: ${(e.tiles || []).join(" ")}`;
      case "new_citizen_auto_optimized": return "성장 후 전체 시민 재최적화";
      case "new_citizen_pending": return `새 시민 ${e.count}명 수동 배치 대기`;
      case "citizens_removed": return `시민 해제: ${(e.tiles || []).join(" ")}`;
      case "food_shortage": return "식량 부족 (저장 식량 0)";
      case "starvation": return `기아로 인구 감소 → ${e.population}`;
      case "boost": return `${label(e.id)} 부스트 ${e.percent}% 적용`;
      default: return `${e.type} ${name}`;
    }
  }
  function presetLabel(id) { return (S.presets.find((p) => p.id === id) || {}).label || id || "-"; }

  function renderBreakdown(yields) {
    const comps = yields.components || [];
    return YK.map((k) => {
      const total = yields.totals[k] || 0;
      const items = comps.filter((c) => Math.abs(c.yields?.[k] || 0) > 1e-9);
      const byCat = {};
      for (const item of items) (byCat[item.category] = byCat[item.category] || []).push(item);
      const body = Object.entries(byCat).map(([cat, list]) => {
        const sum = list.reduce((acc, it) => acc + it.yields[k], 0);
        return `<div class="bd-cat">${esc(CATEGORY_LABEL[cat] || cat)} · ${sgn(sum, 2)}</div>` + list.map((it, i) =>
          `<div class="bd-item" data-bd="${esc(k)}|${esc(comps.indexOf(it))}"><span>${esc(componentLabel(it))}</span><span class="v">${sgn(it.yields[k], 2)}</span></div>`).join("");
      }).join("");
      return `<details class="bd-yield"><summary><span class="yv ${k}">${YL[k]}</span><span class="total">${num(total, 1)}</span></summary>${body || '<div class="bd-item"><span class="muted">구성 요소 없음</span></div>'}</details>`;
    }).join("");
  }
  function componentLabel(c) {
    if (c.kind === "percent_total") return "% 보정 (" + (c.details || []).map((d) => `${d.label} ${sgn(d.percent, 0)}%`).join(", ") + ")";
    return label(c.id) && c.kind !== "worked_tile" ? `${c.label}` : c.label;
  }
  function bindBreakdown(root) {
    const comps = S.detail?.city?.yields?.components || [];
    root.querySelectorAll(".bd-item[data-bd]").forEach((item) => {
      item.onclick = () => {
        const next = item.nextElementSibling;
        if (next && next.classList.contains("bd-detail")) { next.remove(); return; }
        const [, index] = item.dataset.bd.split("|");
        const comp = comps[Number(index)];
        const div = document.createElement("div");
        div.className = "bd-detail";
        div.innerHTML = componentDetail(comp);
        item.after(div);
        if (comp?.tile) {
          const [x, y] = comp.tile.split(",").map(Number);
          state.selectedTile = { x, y }; drawMap(); renderTileDetail();
        }
      };
    });
  }
  function componentDetail(c) {
    if (!c) return "";
    const lines = [];
    if (c.source) lines.push(`출처: ${esc(c.source)}`);
    if (c.attribution) lines.push(`Modifier ${esc(c.attribution.modifier_id)} · ${esc(c.attribution.effect)} · ${esc(c.attribution.source_kind)} ${esc(c.attribution.source_id)}`);
    if (c.tile) lines.push(`타일 (${esc(c.tile)})`);
    for (const d of c.details || []) {
      if (d.percent !== undefined) { lines.push(`${esc(d.label)}: ${sgn(d.percent, 0)}% ${d.source ? `(${esc(d.source)})` : ""}`); continue; }
      const nz = YK.filter((k) => Math.abs(d.yields?.[k] || 0) > 1e-9).map((k) => `${YL[k]} ${sgn(d.yields[k], 2)}`).join(", ");
      lines.push(`${esc(d.label)}${nz ? " — " + esc(nz) : ""}${d.source ? ` · ${esc(d.source)}` : ""}${d.attribution ? ` · ${esc(d.attribution.modifier_id)}` : ""}`);
    }
    return lines.map((l) => `<div>${l}</div>`).join("") || "<div>세부 정보 없음</div>";
  }

  function renderCitySettings(c) {
    const cityDoc = state.cities.find((x) => x.id === c.id) || {};
    const effects = cityDoc.custom_effects || [];
    return `<div class="block"><h4><span>도시 운영 설정 (주거·쾌적도·사용자 효과)</span></h4>
      <div class="form-row"><label>주거공간</label>
        <select id="ovHousingMode"><option value="computed">데이터로 계산</option><option value="manual">수동 값</option></select>
        <label>수동 값</label><input id="ovHousing" type="number" step="0.5" value="${esc(cityDoc.housing ?? 2)}" />
        <label>추가분</label><input id="ovHousingExtra" type="number" step="0.5" value="${esc(cityDoc.housing_extra ?? 0)}" /></div>
      <div class="form-row"><label>추가 쾌적도 (사치·오락 등 수동)</label><input id="ovAmenities" type="number" step="1" value="${esc(cityDoc.amenities ?? 0)}" />
        <label>종교</label><input id="ovReligion" value="${esc(cityDoc.religion || "")}" placeholder="예: RELIGION_BUDDHISM" style="width:150px" /></div>
      <div class="sub">사용자 정의 효과 (총독 등 데이터로 계산되지 않는 효과를 직접 입력 — 게임 데이터가 아님으로 표시됨)</div>
      <table class="grid" id="ovEffects"><thead><tr><th>이름</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}<th class="num">%과학</th><th></th></tr></thead><tbody>
      ${effects.map((e, i) => `<tr data-i="${i}"><td><input data-f="label" value="${esc(e.label)}" style="width:110px" /></td>${YK.map((k) => `<td><input data-f="y:${k}" type="number" step="0.5" value="${esc(e.yields?.[k] ?? 0)}" style="width:48px" /></td>`).join("")}<td><input data-f="p:Science" type="number" value="${esc(e.percent?.Science ?? 0)}" style="width:48px" /></td><td><button class="mini" data-del="${i}" type="button">✕</button></td></tr>`).join("")}
      </tbody></table>
      <div class="form-row"><button type="button" class="mini" id="ovAddEffect">+ 효과 추가</button><button type="button" class="primary" id="ovSaveSettings">운영 설정 적용</button></div></div>
      ${renderTradeRoutes(cityDoc)}
      ${renderIncomingRoutes(cityDoc)}
      ${renderGovernor(cityDoc)}
      ${renderGreatWorks(cityDoc)}`;
  }
  function renderGreatWorks(cityDoc) {
    const works = cityDoc.great_works || [];
    const opts = (S.detail && S.detail.great_work_options) || [];
    const kind = { GREATWORKOBJECT_WRITING: "글", GREATWORKOBJECT_SCULPTURE: "조각", GREATWORKOBJECT_PORTRAIT: "초상화", GREATWORKOBJECT_LANDSCAPE: "풍경화", GREATWORKOBJECT_RELIGIOUS: "종교화", GREATWORKOBJECT_MUSIC: "음악", GREATWORKOBJECT_ARTIFACT: "유물", GREATWORKOBJECT_RELIC: "성물" };
    const nm = (id) => id.replace("GREATWORK_", "").replaceAll("_", " ");
    return `<div class="block"><h4><span>걸작 (직접 입력)</span></h4>
      <p class="note">세이브에서 걸작 위치를 아직 읽지 못합니다. 도시에 있는 걸작을 고르면 GreatWork_YieldChanges(예: 글 걸작 문화 +2)를 더합니다. 테마 보너스·걸작 강화 정책은 계산하지 않습니다.</p>
      ${works.length ? `<ul class="plain">${works.map((w, i) => `<li>${esc(nm(w))} <button class="mini" type="button" data-gw-del="${i}">✕</button></li>`).join("")}</ul>` : '<p class="sub">넣은 걸작 없음</p>'}
      <div class="form-row"><input id="ovGwFilter" placeholder="이름 검색 (예: CHAUCER, RUMI, SUN TZU)" style="width:100%"/></div>
      <div class="form-row"><select id="ovGwPick" style="width:100%">${opts.map((o) => `<option value="${esc(o.id)}">${esc(kind[o.type] || o.type || "")} · ${esc(nm(o.id))}</option>`).join("")}</select><button type="button" class="mini" id="ovGwAdd">+ 걸작 추가</button></div>
    </div>`;
  }
  function renderIncomingRoutes(cityDoc) {
    const routes = cityDoc.incoming_routes || [];
    return `<div class="block"><h4><span>이 도시로 들어오는 교역로 (수입)</span></h4>
      <p class="note">다른 문명·도시국가 상인이 이 도시를 목적지로 둔 교역로입니다(세이브 상인 기록). 도착 도시는 기본적으로 받는 산출이 없고, 세계 의회 무역 조약 같은 …_FROM_OTHERS 효과만 더합니다.</p>
      ${routes.length ? `<ul class="plain">${routes.map((r, i) => `<li>${esc(r.origin_name || r.origin)} <span class="sub">${esc(r.origin_label || "")}${r.started_turn != null ? ` · ${esc(r.started_turn)}턴 시작` : ""}</span> <button class="mini" type="button" data-in-del="${i}">✕</button></li>`).join("")}</ul>` : '<p class="sub">들어오는 교역로 없음</p>'}
    </div>`;
  }
  function renderGovernor(cityDoc) {
    const options = (S.detail && S.detail.governor_options) || [];
    if (!options.length) return "";
    const gov = options.find((g) => g.id === cityDoc.governor);
    const have = new Set(cityDoc.governor_promotions || []);
    const GOV_KO = { GOVERNOR_THE_DEFENDER: "빅토르", GOVERNOR_THE_AMBASSADOR: "아마니", GOVERNOR_THE_CARDINAL: "목샤", GOVERNOR_THE_RESOURCE_MANAGER: "마그누스",
      GOVERNOR_THE_BUILDER: "량", GOVERNOR_THE_EDUCATOR: "핑갈라", GOVERNOR_THE_MERCHANT: "레이나", GOVERNOR_IBRAHIM: "이브라힘" };
    const nm = (id) => GOV_KO[id] || id.replace("GOVERNOR_PROMOTION_", "").replace(/^(EDUCATOR|BUILDER|RESOURCE_MANAGER|MERCHANT|CARDINAL|AMBASSADOR)_/, "").replaceAll("_", " ").toLowerCase();
    const status = !gov ? "" : cityDoc.governor_established === false
      ? `<span class="badge s-partial">정착 중${cityDoc.governor_turns != null ? ` (${cityDoc.governor_turns}/${S.detail.governor_establish_turns || 5}턴)` : ""} · 효과 미적용</span>`
      : '<span class="badge s-verified">정착 완료 · 효과 적용</span>';
    return `<div class="block"><h4><span>총독</span>${status}</h4>
      <p class="note">세이브에서 총독 위치·진급·정착 턴을 읽습니다. 진급 효과(GovernorPromotionModifiers)는 정착을 마친 뒤에만 이 도시에 적용합니다.</p>
      <div class="form-row"><select id="ovGov"><option value="">— 없음 —</option>${options.map((g) => `<option value="${esc(g.id)}" ${g.id === cityDoc.governor ? "selected" : ""}>${esc(nm(g.id))}</option>`).join("")}</select>
        ${gov ? `<label><input type="checkbox" id="ovGovEst" ${cityDoc.governor_established === false ? "" : "checked"}/> 정착 완료</label>` : ""}</div>
      ${gov ? `<ul class="plain gov-promos">${gov.promotions.map((p) => `<li><label><input type="checkbox" data-gov-promo="${esc(p.id)}" ${have.has(p.id) ? "checked" : ""}/> ${esc(nm(p.id))}${p.base ? " (기본)" : ""}</label> <span class="sub">${esc(p.support.status)}</span></li>`).join("")}</ul>` : ""}
    </div>`;
  }
  function renderTradeRoutes(cityDoc) {
    const routes = cityDoc.trade_routes || [];
    const dests = (S.detail && S.detail.trade_destinations) || [];
    const name = (key) => { const d = dests.find((x) => x.key === key); return d ? `${d.name} (${d.kind === "domestic" ? "국내" : "국제" + (d.owner ? " · " + d.owner : "")})` : key; };
    const options = dests.map((d) => `<option value="${esc(d.key)}">${esc(d.name)} — ${d.kind === "domestic" ? "국내" : "국제" + (d.owner ? " · " + esc(d.owner) : "")} (${esc(d.key)})</option>`).join("");
    return `<div class="block"><h4><span>이 도시에서 출발하는 교역로</span></h4>
      <p class="note">세이브의 상인 기록에서 가져오며, 직접 추가·삭제할 수도 있습니다. 목적지 특수지구(District_TradeRouteYields), 도시국가 교역 보너스, 교역로 정책·특성 고정 보너스를 출발 도시 산출에 더합니다. 자원별 보너스는 계산하지 않습니다.</p>
      ${routes.length ? `<ul class="plain">${routes.map((r, i) => `<li>${esc(name(r.destination))} <button class="mini" type="button" data-route-del="${i}">✕</button></li>`).join("")}</ul>` : '<p class="sub">입력된 교역로 없음</p>'}
      ${dests.length ? `<div class="form-row"><select id="ovRouteDest">${options}</select><button type="button" class="mini" id="ovRouteAdd">+ 교역로 추가</button></div>` : '<p class="sub">목적지로 쓸 다른 도시가 없습니다.</p>'}
    </div>`;
  }
  function bindCitySettings(root, c) {
    const cityDoc = state.cities.find((x) => x.id === c.id) || {};
    const mode = root.querySelector("#ovHousingMode"); if (!mode) return;
    mode.value = cityDoc.housing_mode || "computed";
    let effects = JSON.parse(JSON.stringify(cityDoc.custom_effects || []));
    const collect = () => {
      root.querySelectorAll("#ovEffects tbody tr").forEach((tr) => {
        const e = effects[Number(tr.dataset.i)]; if (!e) return;
        e.yields = e.yields || {}; e.percent = e.percent || {};
        tr.querySelectorAll("input[data-f]").forEach((inp) => {
          const f = inp.dataset.f;
          if (f === "label") e.label = inp.value;
          else if (f.startsWith("y:")) e.yields[f.slice(2)] = Number(inp.value) || 0;
          else if (f.startsWith("p:")) e.percent[f.slice(2)] = Number(inp.value) || 0;
        });
      });
    };
    const works = cityDoc.great_works || [];
    const gwAdd = root.querySelector("#ovGwAdd");
    if (gwAdd) gwAdd.onclick = () => saveSettings({ great_works: [...works, root.querySelector("#ovGwPick").value] });
    root.querySelectorAll("[data-gw-del]").forEach((b) => (b.onclick = () => saveSettings({ great_works: works.filter((_, i) => i !== Number(b.dataset.gwDel)) })));
    const gwFilter = root.querySelector("#ovGwFilter");
    if (gwFilter) gwFilter.oninput = () => { const q = gwFilter.value.trim().toUpperCase().replaceAll(" ", "_"); root.querySelectorAll("#ovGwPick option").forEach((o) => { o.hidden = q && !o.value.includes(q); }); const first = [...root.querySelectorAll("#ovGwPick option")].find((o) => !o.hidden); if (first) root.querySelector("#ovGwPick").value = first.value; };
    const routes = (cityDoc.trade_routes || []).map((r) => ({ destination: r.destination, ...(r.started_turn != null ? { started_turn: r.started_turn } : {}) }));
    const addRoute = root.querySelector("#ovRouteAdd");
    if (addRoute) addRoute.onclick = () => saveSettings({ trade_routes: [...routes, { destination: root.querySelector("#ovRouteDest").value }] });
    root.querySelectorAll("[data-route-del]").forEach((b) => (b.onclick = () => saveSettings({ trade_routes: routes.filter((_, i) => i !== Number(b.dataset.routeDel)) })));
    const incoming = cityDoc.incoming_routes || [];
    root.querySelectorAll("[data-in-del]").forEach((b) => (b.onclick = () => saveSettings({ incoming_routes: incoming.filter((_, i) => i !== Number(b.dataset.inDel)) })));
    const govPick = root.querySelector("#ovGov");
    if (govPick) govPick.onchange = () => saveSettings({ governor: govPick.value || null, governor_established: true });
    const govEst = root.querySelector("#ovGovEst");
    if (govEst) govEst.onchange = () => saveSettings({ governor_established: govEst.checked });
    root.querySelectorAll("[data-gov-promo]").forEach((b) => (b.onchange = () => {
      const chosen = [...root.querySelectorAll("[data-gov-promo]")].filter((i) => i.checked).map((i) => i.dataset.govPromo);
      saveSettings({ governor_promotions: chosen });
    }));
    root.querySelector("#ovAddEffect").onclick = () => {
      effects.push({ id: "custom-" + Date.now(), label: "사용자 효과", yields: {}, percent: {} });
      saveSettings({ custom_effects: effects });
    };
    root.querySelectorAll("#ovEffects [data-del]").forEach((b) => (b.onclick = () => { collect(); effects.splice(Number(b.dataset.del), 1); saveSettings({ custom_effects: effects }); }));
    root.querySelector("#ovSaveSettings").onclick = () => {
      collect();
      saveSettings({
        housing_mode: mode.value, housing: Number(root.querySelector("#ovHousing").value),
        housing_extra: Number(root.querySelector("#ovHousingExtra").value), amenities: Number(root.querySelector("#ovAmenities").value),
        religion: root.querySelector("#ovReligion").value.trim() || null, custom_effects: effects,
      });
    };
  }
  async function saveSettings(payload) {
    const c = city(); if (!c) return;
    await guarded(async () => {
      const r = await api(`/api/sim/cities/${encodeURIComponent(c.id)}/settings`, "PUT", payload);
      Object.assign(c, r.city);
      patchTiles(r.map_tiles);
      await refreshAll();
      const diff = YK.map((k) => (r.after[k] - r.before[k])).some((v) => Math.abs(v) > 1e-9);
      status(diff ? `설정 적용 · 변화: ${YK.filter((k) => Math.abs(r.after[k] - r.before[k]) > 1e-9).map((k) => `${YL[k]} ${sgn(r.after[k] - r.before[k], 2)}`).join(", ")}` : "설정 적용 (산출 변화 없음)");
    });
  }
  function patchTiles(tiles) {
    if (!tiles || !state.map) return;
    const index = new Map(state.map.tiles.map((t) => [`${t.x},${t.y}`, t]));
    for (const t of tiles) { const tile = index.get(`${t.x},${t.y}`); if (tile) Object.assign(tile, t); }
    if (typeof clearTileDetailCache === "function") clearTileDetailCache();
  }

  // ----------------------------------------------------------- citizens
  function renderCitizens() {
    const root = $("simCitizens"); if (!root) return;
    const d = S.detail;
    if (!d) { root.innerHTML = '<p class="muted">도시를 선택하세요.</p>'; return; }
    const c = d.city;
    const cityDoc = state.cities.find((x) => x.id === c.id) || {};
    const prefs = cityDoc.tile_preferences || {};
    const custom = cityDoc.custom_strategy || { weights: {}, min_food_surplus: 0 };
    const opt = S.optimize && S.optimize.cityId === c.id ? S.optimize.result : null;
    const tiles = (c.yields.tiles || []).filter((t) => !t.city_center).sort((a, b) => (b.worked - a.worked) || a.key.localeCompare(b.key));
    root.innerHTML = `
      <div class="block"><h4><span>시민 배분 설정</span><span class="sub">${c.yields.citizens_assigned}/${c.population} 배치 · 작업 가능 ${c.yields.capacity}</span></h4>
        <div class="form-row"><label>배분 방식</label><select id="ctMode"><option value="manual">수동 (지도/목록에서 직접)</option><option value="auto">자동 (매 턴 전략으로 최적화)</option></select></div>
        <div class="form-row"><label>운영 전략</label><select id="ctStrategy">${S.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}</select>
          <label>새 시민</label><select id="ctNewCitizen"><option value="auto">자동 배치</option><option value="manual">수동 배치 대기</option></select></div>
        <div class="form-row" id="ctCustom" ${c.strategy === "custom" ? "" : 'style="display:none"'}>${YK.map((k) => `<label>${YL[k]}</label><input type="number" step="0.1" data-w="${k}" value="${esc(custom.weights?.[k] ?? 1)}" style="width:52px" />`).join("")}<label>최소 식량 잉여</label><input type="number" step="0.5" id="ctMinFood" value="${esc(custom.min_food_surplus ?? 0)}" style="width:52px" /></div>
        <div class="form-row"><button type="button" class="primary" id="ctApply">설정 적용</button><button type="button" id="ctPreview">최적 배치 미리보기</button><button type="button" id="ctOptApply">최적 배치 적용</button></div>
        <p class="note">전략은 시민 배치만 바꿉니다(산출량에 임의 보너스를 더하지 않음). 가중치는 최적화용이며 게임 규칙이 아닙니다.</p>
      </div>
      ${opt ? renderOptimize(opt) : ""}
      <div class="block"><h4><span>소유 타일 · 시민 배치</span><span class="sub">지도에서 ‘시민 배치 모드’를 켜고 클릭해도 됩니다</span></h4>
        <table class="grid"><thead><tr><th>타일</th><th>내용</th><th>산출 (1명)</th><th class="num">시민</th><th>우선순위</th></tr></thead><tbody>
        ${tiles.map((t) => {
          const p = prefs[t.key] || {};
          const per = t.district ? t.per_citizen : t.yields;
          const what = t.district ? `${label(t.district)} (슬롯 ${t.specialist_slots})` : [label(t.terrain), t.feature && label(t.feature), t.resource && label(t.resource), t.improvement && (label(t.improvement) + (t.improvement_pillaged ? " (약탈됨)" : ""))].filter(Boolean).join(" · ");
          const slots = t.district ? t.specialist_slots : 1;
          return `<tr data-key="${esc(t.key)}"><td>${esc(t.key)}</td><td>${esc(what)}</td><td>${yieldsInline(per)}</td>
            <td class="num"><div class="row-actions"><button class="mini" data-act="minus" ${t.citizens ? "" : "disabled"}>−</button><b>${t.citizens || 0}</b><button class="mini" data-act="plus" ${slots > (t.citizens || 0) ? "" : "disabled"}>＋</button></div></td>
            <td><div class="row-actions"><button class="mini ${p.lock ? "on" : ""}" data-act="lock" title="항상 우선 선택">🔒</button><button class="mini ${p.block ? "on" : ""}" data-act="block" title="절대 사용 안 함">⛔</button><button class="mini" data-act="pdown">−</button><span>${num(p.priority || 0, 0)}</span><button class="mini" data-act="pup">＋</button></div></td></tr>`;
        }).join("")}
        </tbody></table></div>`;
    root.querySelector("#ctMode").value = c.citizen_mode;
    root.querySelector("#ctStrategy").value = c.strategy || "balanced";
    root.querySelector("#ctNewCitizen").value = c.new_citizen_policy || "auto";
    root.querySelector("#ctStrategy").onchange = (e) => { root.querySelector("#ctCustom").style.display = e.target.value === "custom" ? "" : "none"; };
    const customPayload = () => ({
      weights: Object.fromEntries([...root.querySelectorAll("[data-w]")].map((i) => [i.dataset.w, Number(i.value) || 0])),
      min_food_surplus: Number(root.querySelector("#ctMinFood").value) || 0,
    });
    root.querySelector("#ctApply").onclick = () => {
      const strategy = root.querySelector("#ctStrategy").value;
      saveSettings({ citizen_mode: root.querySelector("#ctMode").value, strategy, new_citizen_policy: root.querySelector("#ctNewCitizen").value, ...(strategy === "custom" ? { custom_strategy: customPayload() } : {}) });
    };
    const optimize = (apply) => guarded(async () => {
      const strategy = root.querySelector("#ctStrategy").value;
      const result = await api(`/api/sim/cities/${encodeURIComponent(c.id)}/optimize`, "POST", { strategy, apply, compare_presets: true, custom_strategy: strategy === "custom" ? customPayload() : null });
      S.optimize = { cityId: c.id, result };
      if (apply) { patchTiles(result.map_tiles); await refreshAll(); } else renderCitizens();
      status(apply ? "최적 배치를 적용했습니다." : "최적 배치 미리보기를 계산했습니다 (적용 안 됨).");
    });
    root.querySelector("#ctPreview").onclick = () => optimize(false);
    root.querySelector("#ctOptApply").onclick = () => optimize(true);
    root.querySelectorAll("tr[data-key]").forEach((tr) => {
      const key = tr.dataset.key;
      const [x, y] = key.split(",").map(Number);
      const t = tiles.find((i) => i.key === key);
      tr.querySelectorAll("[data-act]").forEach((b) => (b.onclick = () => {
        const p = prefs[key] || {};
        const act = b.dataset.act;
        if (act === "plus") assignTile(x, y, (t.citizens || 0) + 1);
        if (act === "minus") assignTile(x, y, Math.max(0, (t.citizens || 0) - 1));
        if (act === "lock") setPref(x, y, { lock: !p.lock, block: p.lock ? p.block : false });
        if (act === "block") setPref(x, y, { block: !p.block, lock: p.block ? p.lock : false });
        if (act === "pup") setPref(x, y, { priority: Math.min(10, (p.priority || 0) + 1) });
        if (act === "pdown") setPref(x, y, { priority: Math.max(-10, (p.priority || 0) - 1) });
      }));
    });
  }
  function renderOptimize(r) {
    const best = {};
    for (const k of YK) best[k] = Math.max(...r.presets.map((p) => p.totals[k]));
    return `<div class="block"><h4><span>최적 배치 결과 · ${esc(r.plan.strategy.label)} ${r.applied ? "(적용됨)" : "(미리보기)"}</span><span class="sub">λ=${esc(r.plan.lagrange_lambda ?? "∞")}</span></h4>
      <table class="grid"><thead><tr><th></th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}</tr></thead><tbody>
        <tr><td>현재 배치</td>${YK.map((k) => `<td class="num">${num(r.current_totals[k], 1)}</td>`).join("")}</tr>
        <tr><td>최적 배치</td>${YK.map((k) => `<td class="num">${num(r.optimized_totals[k], 1)}</td>`).join("")}</tr>
        <tr><td>차이</td>${YK.map((k) => `<td class="num ${cls(r.difference[k])}">${sgn(r.difference[k], 2)}</td>`).join("")}</tr>
      </tbody></table>
      <div class="sub">식량 목표 ${num(r.plan.food_target, 1)} · 예상 식량 ${num(r.plan.projected_food, 1)} ${r.plan.feasible_food_target ? "✓ 충족" : "✕ 불가 — 식량 최대화"}</div>
      <div class="sub" style="margin-top:6px">선택된 타일과 이유</div>
      <ul class="reasons">${r.plan.chosen.map((c) => `<li>${esc(c.key)} ${esc(c.kind === "specialist" ? "전문가" : "")} — ${esc(c.reason)} ${yieldsInline(c.yields)}</li>`).join("")}</ul>
      ${r.plan.alternatives.length ? `<div class="sub">제외된 상위 후보</div><ul class="reasons">${r.plan.alternatives.slice(0, 4).map((c) => `<li>${esc(c.key)} 점수 ${num(c.score, 1)} — ${esc(c.excluded_because)}</li>`).join("")}</ul>` : ""}
      <div class="sub" style="margin-top:6px">전략별 비교 (같은 도시·같은 턴, 실제 배치 계산)</div>
      <table class="grid"><thead><tr><th>전략</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}</tr></thead><tbody>
      ${r.presets.map((p) => `<tr><td>${esc(p.label)}</td>${YK.map((k) => `<td class="num" ${Math.abs(p.totals[k] - best[k]) < 1e-9 ? 'style="font-weight:700;color:#fff"' : ""}>${num(p.totals[k], 1)}</td>`).join("")}</tr>`).join("")}
      </tbody></table><p class="note">${esc(r.optimizer_note)}</p></div>`;
  }
  async function assignTile(x, y, citizens) {
    const c = city(); if (!c) return;
    await guarded(async () => {
      const r = await api(`/api/sim/cities/${encodeURIComponent(c.id)}/citizens/${x}/${y}`, "PUT", { citizens });
      c.citizen_mode = "manual";
      patchTiles(r.map_tiles);
      await refreshAll();
      const changed = YK.filter((k) => Math.abs(r.after[k] - r.before[k]) > 1e-9).map((k) => `${YL[k]} ${sgn(r.after[k] - r.before[k], 2)}`).join(", ");
      status(`(${x}, ${y}) 시민 ${citizens}명 · ${changed || "산출 변화 없음"}`);
    });
  }
  async function setPref(x, y, payload) {
    const c = city(); if (!c) return;
    await guarded(async () => {
      const r = await api(`/api/sim/cities/${encodeURIComponent(c.id)}/tile-preferences/${x}/${y}`, "PUT", payload);
      c.tile_preferences = r.tile_preferences;
      patchTiles(r.map_tiles);
      await refreshAll();
    }, `(${x}, ${y}) 타일 우선순위를 갱신했습니다.`);
  }

  // --------------------------------------------------------- production
  async function loadProductionOptions() {
    const c = city(); if (!c) return;
    try {
      S.prod = { cityId: c.id, ...(await api(`/api/sim/cities/${encodeURIComponent(c.id)}/production/options`)) };
    } catch (error) { S.prod = null; status("생산 목록 오류: " + error.message); }
    renderProduction();
  }
  function renderProduction() {
    const root = $("simProduction"); if (!root) return;
    const d = S.detail;
    if (!d) { root.innerHTML = '<p class="muted">도시를 선택하세요.</p>'; return; }
    const prod = d.city.production;
    const options = S.prod && S.prod.cityId === d.city.id ? S.prod.options : null;
    const filtered = (options || []).filter((o) => (!S.prodFilter.cat || o.category === S.prodFilter.cat) && (!S.prodFilter.onlyAvailable || o.available));
    const sel = state.selectedTile;
    const catLabel = { unit: "유닛", building: "건물", wonder: "불가사의", district: "특수지구", project: "프로젝트" };
    root.innerHTML = `
      <div class="block"><h4><span>생산 대기열</span><span class="sub">생산력 ${num(prod.per_turn, 1)} / 턴 · 초과분 ${esc(S.state?.settings?.overflow_policy || "")}</span></h4>
        ${prod.queue.length ? `<table class="grid"><thead><tr><th>#</th><th>항목</th><th class="num">진행</th><th class="num">보너스</th><th class="num">완성</th><th></th></tr></thead><tbody>
        ${prod.queue.map((e, i) => `<tr><td>${i + 1}</td><td><b>${esc(label(e.target_id))}</b> <span class="sub">${esc(catLabel[e.category] || e.category)}${e.x !== null && e.x !== undefined ? ` (${e.x},${e.y})` : ""}</span>
            <div class="bar prod"><span style="width:${e.progress_percent ?? 0}%"></span></div>
            ${(e.blocked_reasons || []).length ? `<div class="warnbox">${esc(e.blocked_reasons.join("; "))}</div>` : ""}</td>
          <td class="num" title="${esc((e.cost_breakdown || []).map((s) => `${s.step}: ${s.note}`).join("\n"))}">${num(e.progress, 1)} / ${num(e.cost, 1)}</td>
          <td class="num" title="${esc((e.production_flat_parts || []).map((p) => p.label + " +" + p.amount).join(", "))}">${e.production_bonus_percent ? sgn(e.production_bonus_percent, 0) + "%" : (e.production_flat ? "" : "–")}${e.production_flat ? " +" + num(e.production_flat, 0) : ""}</td>
          <td class="num">${e.estimated_turns ? e.estimated_turns + "턴" : "–"}</td>
          <td><div class="row-actions"><button class="mini" data-up="${i}" ${i ? "" : "disabled"}>↑</button><button class="mini" data-down="${i}" ${i < prod.queue.length - 1 ? "" : "disabled"}>↓</button><button class="mini" data-rm="${esc(e.queue_id)}">✕</button></div></td></tr>`).join("")}
        </tbody></table>` : '<p class="muted">대기열이 비어 있습니다.</p>'}
        <p class="note">완성 예상 턴은 현재 생산력·보너스·초과분 이월을 가정하며, 생산력이 바뀌면 다시 계산됩니다. 비용에 마우스를 올리면 비용 계산 단계를 볼 수 있습니다.</p>
      </div>
      <div class="block"><h4><span>생산 대상 추가</span></h4>
        <div class="form-row"><select id="pdCat"><option value="">전체</option>${Object.entries(catLabel).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
          <label><input type="checkbox" id="pdAvail" ${S.prodFilter.onlyAvailable ? "checked" : ""}/> 생산 가능 항목만</label></div>
        ${options ? `<div class="form-row"><select id="pdItem" style="width:100%">${filtered.map((o) => `<option value="${esc(o.category)}|${esc(o.id)}" ${o.available ? "" : "disabled"}>${esc(catLabel[o.category])} · ${esc(label(o.id))} · ${num(o.cost, 0)}${o.production_bonus_percent ? ` (+${num(o.production_bonus_percent, 0)}%)` : ""}${o.available ? "" : " — " + esc(o.reasons[0] || "")}</option>`).join("")}</select></div>
        <div class="form-row"><button type="button" class="primary" id="pdAdd">대기열에 추가</button><span class="sub">특수지구·불가사의 배치 타일: ${sel ? `(${sel.x}, ${sel.y})` : "지도에서 선택"}</span></div>
        <div id="pdInfo" class="note"></div>` : '<p class="muted">생산 가능 목록을 불러오는 중…</p>'}
        <ul class="reasons">${(S.prod?.cost_notes || []).map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
      </div>`;
    root.querySelector("#pdCat").value = S.prodFilter.cat;
    root.querySelector("#pdCat").onchange = (e) => { S.prodFilter.cat = e.target.value; renderProduction(); };
    root.querySelector("#pdAvail").onchange = (e) => { S.prodFilter.onlyAvailable = e.target.checked; renderProduction(); };
    const itemSel = root.querySelector("#pdItem");
    const info = () => {
      const [cat, id] = (itemSel?.value || "|").split("|");
      const o = (options || []).find((x) => x.category === cat && x.id === id);
      const box = root.querySelector("#pdInfo");
      if (box) box.innerHTML = o ? `비용: ${(o.cost_breakdown || []).map((s) => esc(s.note) + (s.value !== undefined ? ` = ${num(s.value, 1)}` : "")).join(" → ")}${o.production_bonus.length ? `<br>보너스: ${o.production_bonus.map((b) => esc(label(b.label)) + " " + sgn(b.percent, 0) + "%").join(", ")}` : ""}${o.reasons.length ? `<br>조건: ${esc(o.reasons.join("; "))}` : ""}` : "";
    };
    if (itemSel) { itemSel.onchange = info; info(); }
    const add = root.querySelector("#pdAdd");
    if (add) add.onclick = () => {
      const [category, target_id] = itemSel.value.split("|");
      const body = { category, target_id };
      if (category === "district" || category === "wonder") {
        if (!state.selectedTile) { toast("지도에서 배치할 타일을 먼저 선택하세요."); return; }
        body.x = state.selectedTile.x; body.y = state.selectedTile.y;
      }
      queueRequest(`/production/queue`, "POST", body, `${label(target_id)} 대기열 추가`);
    };
    root.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => queueRequest(`/production/queue/${encodeURIComponent(b.dataset.rm)}`, "DELETE", undefined, "생산 취소")));
    root.querySelectorAll("[data-up],[data-down]").forEach((b) => (b.onclick = () => {
      const i = Number(b.dataset.up ?? b.dataset.down);
      const pos = b.dataset.up !== undefined ? i - 1 : i + 1;
      queueRequest(`/production/order`, "PUT", { queue_id: prod.queue[i].queue_id, position: pos }, "대기열 순서 변경");
    }));
  }
  async function queueRequest(suffix, method, body, msg) {
    const c = city(); if (!c) return;
    await guarded(async () => {
      await api(`/api/sim/cities/${encodeURIComponent(c.id)}${suffix}`, method, body);
      await reloadDocs();
      S.prod = null;
      await refreshAll();
      if (document.querySelector('[data-tabs="right"] .tab.active')?.dataset.tab === "production") await loadProductionOptions();
    }, msg);
  }

  // ------------------------------------------------------------- empire
  async function loadEmpire() {
    try { S.empire = await api("/api/sim/empire/options"); } catch (error) { status("정책 정보 오류: " + error.message); }
    renderEmpire();
  }
  function renderEmpire() {
    const root = $("simEmpirePanel"); if (!root || !S.empire) return;
    const e = S.empire, cur = e.current;
    const gov = e.governments.find((g) => g.id === cur.government);
    const slots = gov ? gov.slots : {};
    const slotName = { SLOT_MILITARY: "군사", SLOT_ECONOMIC: "경제", SLOT_DIPLOMATIC: "외교", SLOT_WILDCARD: "와일드카드", SLOT_GREAT_PERSON: "위인" };
    const supportBadge = (s) => `<span class="badge ${s.status.startsWith("효과 해석") ? "s-unverified" : s.status === "부분 지원" ? "s-partial" : s.status === "효과 없음" ? "s-unverified" : "s-unsupported"}" title="지원 ${s.supported.length} / ${s.total}">${esc(s.status)}</span>`;
    const policyOptions = (slot, chosen) => {
      const list = e.policies.filter((p) => slot === "SLOT_WILDCARD" || p.slot === slot);
      return `<option value="">— 비어 있음 —</option>` + list.map((p) => `<option value="${esc(p.id)}" ${p.id === chosen ? "selected" : ""}>${esc(label(p.id))}${p.available ? "" : " (사회제도 미완료)"}${p.obsolete_now ? " (폐기됨)" : ""}</option>`).join("");
    };
    const civ = e.civilizations.find((c) => c.id === cur.civilization);
    const leaders = civ ? e.leaders.filter((l) => civ.leaders.includes(l.id)) : e.leaders;
    const chosenPolicies = Object.values(cur.policy_slots || {}).flat();
    root.innerHTML = `
      <div class="block"><h4><span>정부와 정책 카드</span></h4>
        <div class="form-row"><label>정부</label><select id="emGov"><option value="">— 없음 —</option>${e.governments.map((g) => `<option value="${esc(g.id)}" ${g.id === cur.government ? "selected" : ""}>${esc(label(g.id))}${g.available ? "" : " (사회제도 필요)"}</option>`).join("")}</select>${gov ? supportBadge(gov.support) : ""}</div>
        ${Object.entries(slots).map(([slot, n]) => `<div class="form-row"><label>${esc(slotName[slot] || slot)} ×${n}</label>${Array.from({ length: n }, (_, i) => `<select data-slot="${esc(slot)}" data-i="${i}">${policyOptions(slot, (cur.policy_slots?.[slot] || [])[i])}</select>`).join("")}</div>`).join("") || '<p class="note">정부를 선택하면 슬롯이 표시됩니다.</p>'}
        <div class="form-row"><label><input type="checkbox" id="emEnforce" ${cur.enforce_prerequisites !== false ? "checked" : ""}/> 기술·사회제도 선행 조건 확인 (끄면 생산·정부 조건 검사 생략)</label></div>
        <div class="form-row"><label><input type="checkbox" id="emAmenity" ${cur.apply_amenity_effects !== false ? "checked" : ""}/> 이 게임에서 쾌적도 효과(성장·산출 %) 적용</label></div>
        ${chosenPolicies.map((pid) => { const p = e.policies.find((x) => x.id === pid); return p ? `<div class="form-row"><span class="chip">${esc(label(pid))}</span>${supportBadge(p.support)} <span class="sub">${p.support.supported.map((s) => esc(s.effect.replace("EFFECT_", ""))).join(", ")}</span></div>` : ""; }).join("")}
      </div>
      <div class="block"><h4><span>문명 · 지도자 · 종교</span></h4>
        <div class="form-row"><label>문명</label><select id="emCiv"><option value="">— 수도 도시 설정 사용 —</option>${e.civilizations.map((c) => `<option value="${esc(c.id)}" ${c.id === cur.civilization ? "selected" : ""}>${esc(label(c.id))}</option>`).join("")}</select>${civ ? supportBadge(civ.support) : ""}</div>
        <div class="form-row"><label>지도자</label><select id="emLeader"><option value="">— 없음 —</option>${leaders.map((l) => `<option value="${esc(l.id)}" ${l.id === cur.leader ? "selected" : ""}>${esc(label(l.id))}</option>`).join("")}</select>${(() => { const l = e.leaders.find((x) => x.id === cur.leader); return l ? supportBadge(l.support) : ""; })()}</div>
        ${civ ? `<div class="sub">고유: ${esc([...civ.unique.units, ...civ.unique.buildings, ...civ.unique.districts, ...civ.unique.improvements].map(label).join(", ") || "없음")}</div>` : ""}
        <div class="form-row"><label>판테온</label><select id="emPantheon"><option value="">— 없음 —</option>${e.beliefs.filter((b) => b.class === "BELIEF_CLASS_PANTHEON").map((b) => `<option value="${esc(b.id)}" ${b.id === cur.pantheon ? "selected" : ""}>${esc(label(b.id))} · ${esc(b.support.status)}</option>`).join("")}</select></div>
      </div>
      ${renderCityStates(e, cur)}
      ${renderCongress(e, cur)}
      ${e.governors && e.governors.available ? "" : `<div class="block"><h4><span>총독</span>${badge("미지원")}</h4><p class="note">${esc((e.governors || {}).reason || "")}</p></div>`}
      <div class="form-row"><button type="button" class="primary" id="emApply">정책·문명 설정 적용 (모든 도시 즉시 재계산)</button></div>
      ${S.empireResult ? renderEmpireImpact(S.empireResult) : ""}
      <p class="note">‘효과 해석 지원’은 해당 항목의 모든 Modifier 효과 유형을 엔진이 해석한다는 뜻이며, 게임과 대조 검증되었다는 뜻이 아닙니다. 해석하지 못한 효과는 ‘계산 근거·미지원 규칙’ 탭의 감사 로그에 남습니다.</p>`;
    const readForm = () => {
      const policy_slots = {};
      root.querySelectorAll("[data-slot]").forEach((s) => { if (s.value) (policy_slots[s.dataset.slot] = policy_slots[s.dataset.slot] || []).push(s.value); });
      return {
        government: root.querySelector("#emGov").value || null, policy_slots,
        civilization: root.querySelector("#emCiv").value || null, leader: root.querySelector("#emLeader").value || null,
        pantheon: root.querySelector("#emPantheon").value || null, enforce_prerequisites: root.querySelector("#emEnforce").checked,
        apply_amenity_effects: root.querySelector("#emAmenity").checked,
        city_states: readCityStates(root),
        world_congress: readCongress(root),
      };
    };
    // Keep unsaved choices when the form re-renders (government/civilization change).
    root.querySelector("#emGov").onchange = () => { Object.assign(S.empire.current, readForm(), { policy_slots: {} }); renderEmpire(); };
    root.querySelector("#emCiv").onchange = () => { Object.assign(S.empire.current, readForm(), { leader: null }); renderEmpire(); };
    root.querySelectorAll("#emWcRows [data-wc-del]").forEach((b) => (b.onclick = () => { const tr = b.closest("tr"); tr.dataset.removed = "1"; tr.style.display = "none"; }));
    const wcAdd = root.querySelector("#emWcAdd");
    if (wcAdd) wcAdd.onclick = () => {
      const id = root.querySelector("#emWcPick").value;
      const kind = ((S.empire.resolutions || []).find((r) => r.id === id) || {}).target_kind;
      Object.assign(S.empire.current, readForm());
      S.empire.current.world_congress = [...(S.empire.current.world_congress || []), { resolution: id, effect: 1, targets_me: kind === "PLAYER", enabled: true, target_kind: kind }];
      renderEmpire();
    };
    root.querySelector("#emApply").onclick = () => guarded(async () => {
      const payload = readForm();
      S.empireResult = await api("/api/sim/empire", "PUT", payload);
      S.empire = null; await loadEmpire();
      await refreshAll();
      status("정책·문명 설정을 적용했습니다." + (S.empireResult.warnings.length ? " 경고: " + S.empireResult.warnings.join(" / ") : ""));
    });
  }
  const CS_TYPE = { INDUSTRIAL: "산업", SCIENTIFIC: "과학", CULTURAL: "문화", RELIGIOUS: "종교", TRADE: "상업", MILITARISTIC: "군사" };
  function renderCityStates(e, cur) {
    const list = e.city_states || [];
    if (!list.length) return `<div class="block"><h4><span>도시국가 사절</span></h4><p class="note">세이브에서 가져온 게임에만 도시국가 목록이 있습니다.</p></div>`;
    const entered = cur.city_states || Object.fromEntries(list.map((c) => [c.civilization, { envoys: c.envoys, suzerain: c.suzerain }]));
    const rows = [...list].sort((a, b) => (a.type || "").localeCompare(b.type || "") || a.name.localeCompare(b.name)).map((c) => {
      const v = entered[c.civilization] || {};
      return `<tr><td>${esc(c.name)}</td><td>${esc(CS_TYPE[c.type] || c.type || "-")}</td>
        <td><input type="number" min="0" max="99" step="1" style="width:56px" data-cs="${esc(c.civilization)}" value="${Number(v.envoys || 0)}"/></td>
        <td><input type="checkbox" data-cs-suz="${esc(c.civilization)}" ${v.suzerain ? "checked" : ""}/></td></tr>`;
    }).join("");
    return `<div class="block"><h4><span>도시국가 사절 (직접 입력)</span></h4>
      <p class="note">세이브에서 사절 수를 읽지 못합니다. 사절 1·3·6명 유형 보너스와 종주국 고유 보너스를 데이터(MINOR_CIV_*_TRAIT)대로 적용합니다. 평화 상태로 가정하며, 동맹·종교 조건이 붙은 보너스는 제외됩니다.</p>
      <table class="grid"><thead><tr><th>도시국가</th><th>유형</th><th>사절</th><th>종주국</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  function renderCongress(e, cur) {
    const list = cur.world_congress || [];
    const res = e.resolutions || [];
    if (!res.length) return "";
    const WC_KO = { DIPLOVICTORY: "외교 승리 점수", LUXURY: "사치품", TRADE_TREATY: "무역 조약", WORLD_RELIGION: "세계 종교", MERCENARY_COMPANIES: "용병",
      ARMS_CONTROL: "군비 통제", HERITAGE_ORG: "문화유산 기구", WORLD_IDEOLOGY: "세계 이념", URBAN_DEVELOPMENT: "도시 개발", BORDER_CONTROL: "국경 통제",
      PUBLIC_WORKS: "공공사업", PATRONAGE: "후원", GLOBAL_ENERGY_TREATY: "세계 에너지 조약", SOVEREIGNTY: "주권", MIGRATION_TREATY: "이민 조약",
      DEFORESTATION_TREATY: "산림 벌채 조약", PUBLIC_RELATIONS: "홍보", ESPIONAGE_PACT: "첩보 협정", MILITARY_ADVISORY: "군사 자문" };
    const nm = (id) => { const k = String(id).replace("WC_RES_", ""); return WC_KO[k] ? `${WC_KO[k]} (${k})` : k.replaceAll("_", " "); };
    const rows = list.map((r, i) => `<tr data-wc="${i}"><td><input type="checkbox" data-wc-on ${r.enabled === false ? "" : "checked"}/></td><td>${esc(nm(r.resolution))}<div class="sub">${esc(r.target_kind || "")}${r.votes ? ` · 표 A ${esc(r.votes.A)} / B ${esc(r.votes.B)}` : ""}${r.source === "save" ? " · 세이브에서 읽음(추정)" : ""}</div></td>
      <td><select data-wc-eff><option value="1" ${r.effect === 1 ? "selected" : ""}>A</option><option value="2" ${r.effect === 2 ? "selected" : ""}>B</option></select></td>
      <td><input type="checkbox" data-wc-me ${r.targets_me ? "checked" : ""}/></td><td><button type="button" class="mini" data-wc-del>✕</button></td></tr>`).join("");
    return `<div class="block"><h4><span>세계 의회 결의</span></h4>
      <p class="note">대상이 플레이어인 결의 중 ‘내가 대상’인 것만 적용합니다(예: 무역 조약 A → 이 문명 도시로 들어오는 교역로마다 금 +4). 자원·위인 등 다른 대상 결의는 목록에만 남고 계산하지 않습니다.</p>
      <table class="grid"><thead><tr><th>적용</th><th>결의</th><th>결과</th><th>내가 대상</th><th></th></tr></thead><tbody id="emWcRows">${rows || '<tr><td colspan="5" class="sub">결의 없음</td></tr>'}</tbody></table>
      <div class="form-row"><select id="emWcPick">${res.map((r) => `<option value="${esc(r.id)}">${esc(nm(r.id))} (${esc(r.target_kind)})</option>`).join("")}</select><button type="button" class="mini" id="emWcAdd">+ 결의 추가</button></div></div>`;
  }
  function readCongress(root) {
    const base = (S.empire && S.empire.current.world_congress) || [];
    const result = [];
    root.querySelectorAll("#emWcRows tr[data-wc]").forEach((tr) => {
      const item = base[Number(tr.dataset.wc)]; if (!item || tr.dataset.removed) return;
      result.push({ ...item, enabled: tr.querySelector("[data-wc-on]").checked, effect: Number(tr.querySelector("[data-wc-eff]").value), targets_me: tr.querySelector("[data-wc-me]").checked });
    });
    return result;
  }
  function readCityStates(root) {
    const result = {};
    root.querySelectorAll("[data-cs]").forEach((i) => { const n = Math.max(0, Math.floor(Number(i.value) || 0)); if (n) result[i.dataset.cs] = { envoys: n, suzerain: false }; });
    root.querySelectorAll("[data-cs-suz]").forEach((i) => { if (i.checked) result[i.dataset.csSuz] = { envoys: (result[i.dataset.csSuz] || {}).envoys || 0, suzerain: true }; });
    return result;
  }
  function renderEmpireImpact(r) {
    return `<div class="block"><h4><span>설정 변경 영향</span></h4>
      ${r.warnings.map((w) => `<div class="warnbox">${esc(w)}</div>`).join("")}
      <table class="grid"><thead><tr><th>도시</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}<th class="num">주거</th><th class="num">쾌적</th></tr></thead><tbody>
      ${r.impact.map((i) => `<tr><td>${esc(i.name)}</td>${YK.map((k) => `<td class="num ${cls(i.delta[k])}">${sgn(i.delta[k], 2)}</td>`).join("")}<td class="num ${cls(i.housing_delta)}">${sgn(i.housing_delta, 1)}</td><td class="num ${cls(i.amenity_delta)}">${sgn(i.amenity_delta, 0)}</td></tr>`).join("")}
      </tbody></table><div class="sub">Modifier 적용 ${r.modifiers.applied_effects} · 미지원/건너뜀 ${r.modifiers.skipped}</div></div>`;
  }

  // --------------------------------------------------------------- tile
  function renderTileAdjust(owner, key) {
    const adj = ((owner.tile_adjustments || {})[key]) || { yields: {} };
    return `<details class="tile-adjust"${Object.keys(adj.yields || {}).length ? " open" : ""}><summary>게임 값과 다를 때: 이 타일 수동 보정</summary>
      <p class="note">범람·화산·폭풍으로 늘어난 산출은 세이브에서 읽어 이미 들어가 있습니다. 그래도 게임 툴팁과 다르면 차이만큼 입력하세요. 이 타일 산출에 더하고 장부에 ‘수동 보정’으로 따로 표시합니다.</p>
      <div class="form-row">${YK.map((k) => `<label>${YL[k]} <input type="number" step="1" style="width:48px" data-adj="${k}" value="${Number((adj.yields || {})[k] || 0)}"/></label>`).join("")}</div>
      <div class="form-row"><input id="tdAdjNote" placeholder="메모 (예: 약탈된 농장)" value="${esc(adj.note || "")}" style="width:60%"/><button class="mini" type="button" id="tdAdjSave">보정 저장</button><button class="mini" type="button" id="tdAdjClear">보정 삭제</button></div></details>`;
  }
  function bindTileAdjust(root, owner, key) {
    const save = root.querySelector("#tdAdjSave"); if (!save) return;
    const all = { ...(owner.tile_adjustments || {}) };
    save.onclick = () => {
      const yields = {};
      root.querySelectorAll("[data-adj]").forEach((i) => { const v = Number(i.value) || 0; if (v) yields[i.dataset.adj] = v; });
      if (Object.keys(yields).length) all[key] = { yields, note: root.querySelector("#tdAdjNote").value.trim() }; else delete all[key];
      void saveSettings({ tile_adjustments: all });
    };
    root.querySelector("#tdAdjClear").onclick = () => { delete all[key]; void saveSettings({ tile_adjustments: all }); };
  }
  function renderTileDetail() {
    const root = $("simTileDetail"); if (!root) return;
    const sel = state.selectedTile;
    if (!sel) { root.innerHTML = '<p class="muted">지도에서 타일을 선택하세요.</p>'; return; }
    const key = `${sel.x},${sel.y}`;
    const info = S.tiles.get(key);
    const owner = info ? state.cities.find((c) => c.id === info.cityId) : null;
    if (!info) { root.innerHTML = `<div class="block"><h4><span>타일 (${esc(key)})</span></h4><p class="muted">도시 소유 타일이 아닙니다. 산출 계산은 소유 타일만 합니다. 아래 기존 편집기로 지형을 바꿀 수 있습니다.</p></div>`; return; }
    const comps = info.components || [];
    root.innerHTML = `<div class="block"><h4><span>타일 (${esc(key)}) · ${esc(owner?.name || "")}</span><span class="sub">${info.city_center ? "도심" : info.worked ? `시민 ${info.citizens || 1}명` : "미작업"}</span></h4>
      ${info.district ? `<div class="kv"><span>특수지구</span><span>${esc(label(info.district))}</span><span>전문가 슬롯</span><span>${esc(info.specialist_slots)}</span><span>시민 1명 산출</span><span>${yieldsInline(info.per_citizen)}</span>${info.adjacency ? `<span>인접 보너스</span><span>${yieldsInline(info.adjacency)}</span>` : ""}</div>`
        : `${yieldsInline(info.yields, 2)}<ul class="reasons">${comps.map((c) => `<li>${esc(c.label)} ${YK.filter((k) => Math.abs(c.yields[k]) > 1e-9).map((k) => `${YL[k]} ${sgn(c.yields[k], 2)}`).join(", ")}${c.source ? ` <span class="sub">(${esc(c.source)})</span>` : ""}</li>`).join("")}</ul>`}
      ${info.pref ? `<div class="sub">우선순위: ${info.pref.lock ? "🔒 잠금 " : ""}${info.pref.block ? "⛔ 사용 금지 " : ""}${info.pref.priority ? "가중 " + info.pref.priority : ""}</div>` : ""}
      ${owner && owner.id === state.selectedCityId && !info.district ? renderTileAdjust(owner, key) : ""}
      ${owner && owner.id === state.selectedCityId && !info.city_center ? `<div class="form-row"><button class="mini" id="tdPlus">시민 배치 +1</button><button class="mini" id="tdMinus">시민 해제 −1</button><button class="mini" id="tdLock">🔒 잠금 전환</button><button class="mini" id="tdBlock">⛔ 금지 전환</button></div>` : ""}</div>`;
    if (owner) bindTileAdjust(root, owner, key);
    const plus = root.querySelector("#tdPlus");
    if (plus) {
      const p = info.pref || {};
      plus.onclick = () => assignTile(sel.x, sel.y, (info.citizens || 0) + 1);
      root.querySelector("#tdMinus").onclick = () => assignTile(sel.x, sel.y, Math.max(0, (info.citizens || 0) - 1));
      root.querySelector("#tdLock").onclick = () => setPref(sel.x, sel.y, { lock: !p.lock, block: false });
      root.querySelector("#tdBlock").onclick = () => setPref(sel.x, sel.y, { block: !p.block, lock: false });
    }
  }

  // ------------------------------------------------------------ map hooks
  function overlayActive() { return S.overlay !== "normal" && S.overlay !== "details"; }
  // Short Korean names for the tile detail overlay.
  const SHORT = {
    TERRAIN_GRASS: "초원", TERRAIN_GRASS_HILLS: "초원언덕", TERRAIN_GRASS_MOUNTAIN: "산", TERRAIN_PLAINS: "평원", TERRAIN_PLAINS_HILLS: "평원언덕",
    TERRAIN_PLAINS_MOUNTAIN: "산", TERRAIN_DESERT: "사막", TERRAIN_DESERT_HILLS: "사막언덕", TERRAIN_DESERT_MOUNTAIN: "산",
    TERRAIN_TUNDRA: "툰드라", TERRAIN_TUNDRA_HILLS: "툰드라언덕", TERRAIN_TUNDRA_MOUNTAIN: "산", TERRAIN_SNOW: "설원", TERRAIN_SNOW_HILLS: "설원언덕",
    TERRAIN_SNOW_MOUNTAIN: "산", TERRAIN_COAST: "연안", TERRAIN_OCEAN: "바다",
    FEATURE_FOREST: "숲", FEATURE_JUNGLE: "정글", FEATURE_MARSH: "습지", FEATURE_OASIS: "오아시스", FEATURE_FLOODPLAINS: "범람원",
    FEATURE_FLOODPLAINS_GRASSLAND: "범람원", FEATURE_FLOODPLAINS_PLAINS: "범람원", FEATURE_REEF: "암초", FEATURE_ICE: "빙하", FEATURE_GEOTHERMAL_FISSURE: "지열",
    FEATURE_VOLCANIC_SOIL: "화산토",
    IMPROVEMENT_FARM: "농장", IMPROVEMENT_MINE: "광산", IMPROVEMENT_QUARRY: "채석장", IMPROVEMENT_PLANTATION: "플랜테이션", IMPROVEMENT_CAMP: "캠프",
    IMPROVEMENT_PASTURE: "목장", IMPROVEMENT_FISHING_BOATS: "어선", IMPROVEMENT_LUMBER_MILL: "제재소", IMPROVEMENT_OIL_WELL: "유정",
    IMPROVEMENT_OFFSHORE_OIL_RIG: "해상유정", IMPROVEMENT_FORT: "요새", IMPROVEMENT_BEACH_RESORT: "해변휴양지", IMPROVEMENT_SKI_RESORT: "스키장",
    DISTRICT_CITY_CENTER: "도심", DISTRICT_CAMPUS: "캠퍼스", DISTRICT_THEATER: "극장가", DISTRICT_HOLY_SITE: "성지", DISTRICT_ENCAMPMENT: "주둔지",
    DISTRICT_COMMERCIAL_HUB: "상업중심지", DISTRICT_HARBOR: "항만", DISTRICT_INDUSTRIAL_ZONE: "산업구역", DISTRICT_ENTERTAINMENT_COMPLEX: "유흥단지",
    DISTRICT_WATER_ENTERTAINMENT_COMPLEX: "수상공원", DISTRICT_AQUEDUCT: "송수로", DISTRICT_NEIGHBORHOOD: "주거지구", DISTRICT_AERODROME: "비행장",
    DISTRICT_SPACEPORT: "우주기지", DISTRICT_GOVERNMENT: "정부청사", DISTRICT_DIPLOMATIC_QUARTER: "외교지구", DISTRICT_PRESERVE: "보호구역",
    DISTRICT_DAM: "댐", DISTRICT_CANAL: "운하", DISTRICT_BATH: "목욕탕", DISTRICT_BASILIKOI_PAIDES: "바실리코이",
    BUILDING_PALACE: "궁전", BUILDING_MONUMENT: "기념비", BUILDING_GRANARY: "곡창", BUILDING_WALLS: "성벽", BUILDING_CASTLE: "중세성벽",
    BUILDING_STAR_FORT: "르네상스성벽", BUILDING_WATER_MILL: "물레방앗간", BUILDING_SEWER: "하수도", BUILDING_LIBRARY: "도서관",
    BUILDING_UNIVERSITY: "대학", BUILDING_RESEARCH_LAB: "연구소", BUILDING_MARKET: "시장", BUILDING_BANK: "은행", BUILDING_STOCK_EXCHANGE: "증권거래소",
    BUILDING_WORKSHOP: "작업장", BUILDING_FACTORY: "공장", BUILDING_POWER_PLANT: "발전소", BUILDING_SHRINE: "사당", BUILDING_TEMPLE: "사원",
    BUILDING_AMPHITHEATER: "원형극장", BUILDING_MUSEUM_ART: "미술관", BUILDING_MUSEUM_ARTIFACT: "고고학박물관", BUILDING_BROADCAST_CENTER: "방송센터",
    BUILDING_ARENA: "경기장", BUILDING_ZOO: "동물원", BUILDING_STADIUM: "스타디움", BUILDING_BARRACKS: "병영", BUILDING_STABLE: "마구간",
    BUILDING_BASILIKOI_PAIDES: "바실리코이", BUILDING_ARMORY: "무기고", BUILDING_MILITARY_ACADEMY: "군사학교", BUILDING_LIGHTHOUSE: "등대",
    BUILDING_SHIPYARD: "조선소", BUILDING_SEAPORT: "항구", BUILDING_CONSULATE: "영사관", BUILDING_CHANCERY: "상서원",
    BUILDING_GOV_TALL: "알현실", BUILDING_GOV_WIDE: "조상의전당", BUILDING_GOV_CONQUEST: "군주의옥좌",
    RESOURCE_RICE: "쌀", RESOURCE_WHEAT: "밀", RESOURCE_MAIZE: "옥수수", RESOURCE_BANANAS: "바나나", RESOURCE_CATTLE: "소", RESOURCE_SHEEP: "양",
    RESOURCE_DEER: "사슴", RESOURCE_STONE: "돌", RESOURCE_COPPER: "구리", RESOURCE_FISH: "물고기", RESOURCE_CRABS: "게", RESOURCE_SUGAR: "설탕",
    RESOURCE_SPICES: "향신료", RESOURCE_TRUFFLES: "송로버섯", RESOURCE_WINE: "와인", RESOURCE_SILK: "비단", RESOURCE_COTTON: "목화",
    RESOURCE_COFFEE: "커피", RESOURCE_TEA: "차", RESOURCE_TOBACCO: "담배", RESOURCE_CITRUS: "감귤", RESOURCE_COCOA: "코코아", RESOURCE_DYES: "염료",
    RESOURCE_INCENSE: "향", RESOURCE_IVORY: "상아", RESOURCE_FURS: "모피", RESOURCE_SALT: "소금", RESOURCE_SILVER: "은", RESOURCE_GYPSUM: "석고",
    RESOURCE_JADE: "옥", RESOURCE_MARBLE: "대리석", RESOURCE_MERCURY: "수은", RESOURCE_DIAMONDS: "다이아몬드", RESOURCE_PEARLS: "진주",
    RESOURCE_WHALES: "고래", RESOURCE_HONEY: "꿀", RESOURCE_OLIVES: "올리브", RESOURCE_AMBER: "호박", RESOURCE_HORSES: "말", RESOURCE_IRON: "철",
    RESOURCE_NITER: "초석", RESOURCE_COAL: "석탄", RESOURCE_OIL: "석유", RESOURCE_ALUMINUM: "알루미늄", RESOURCE_URANIUM: "우라늄",
    FEATURE_JUNGLE_: "정글",
  };
  const shortName = (id) => (id ? SHORT[id] || label(id).replace(/^(TERRAIN|FEATURE|IMPROVEMENT|DISTRICT|BUILDING|RESOURCE)_/, "").replaceAll("_", " ") : "");
  const YDOT = { Food: "#7ac36a", Production: "#e0954f", Gold: "#f0cc4f", Science: "#6fb4f0", Culture: "#cc86e2", Faith: "#e6dfc4" };
  // True when the tile detail overlay draws this tile (the map then skips its own glyphs).
  function detailsAt(t, size) { return S.overlay === "details" && size > 14 && S.tiles.has(`${t.x},${t.y}`); }
  function drawTileDetails(ctx, info, p, size) {
    const fs = Math.max(8, Math.min(14, size * 0.21));
    const full = size >= 44, mid = size >= 28; // more lines as the map is zoomed in
    const lines = [];
    if (info.city_center) {
      const c = state.cities.find((x) => x.id === info.cityId);
      ctx.beginPath(); ctx.arc(p.x, p.y, size * 0.62, 0, Math.PI * 2); ctx.fillStyle = "rgba(60,44,12,0.55)"; ctx.fill();
      ctx.strokeStyle = "#d6ad59"; ctx.lineWidth = 1.5; ctx.stroke();
      if (c && mid) lines.push({ text: `${c.name} (${c.population})`, bold: true, color: "#ffe39a" });
    } else if (info.district) {
      ctx.beginPath(); ctx.arc(p.x, p.y, size * 0.62, 0, Math.PI * 2); ctx.fillStyle = "rgba(18,40,70,0.5)"; ctx.fill();
    }
    if (info.district) {
      if (!info.city_center && mid) lines.push({ text: shortName(info.district), bold: true, color: "#ffe39a" });
      const adj = info.adjacency ? YK.filter((k) => info.adjacency[k]).map((k) => ({ k, v: info.adjacency[k] })) : [];
      if (adj.length) lines.push({ yields: adj, prefix: "인접 " });
      if (info.city_center) lines.push({ yields: YK.filter((k) => info.yields?.[k]).map((k) => ({ k, v: info.yields[k] })) });
      if (!mid) { /* yields / adjacency only */ } else if (info.buildings && info.buildings.length && full) {
        const names = info.buildings.map((b) => shortName(b).slice(0, 6));
        lines.push({ text: names.slice(0, 3).join("·") + (names.length > 3 ? ` +${names.length - 3}` : ""), color: "#d7e3ea" });
      } else if (info.buildings && info.buildings.length) lines.push({ text: `건물 ${info.buildings.length}`, color: "#d7e3ea" });
    } else {
      const terr = [shortName(info.terrain), shortName(info.feature)].filter(Boolean).join("·");
      if (mid) lines.push({ text: terr, color: "#e8eef1" });
      // Improvement (and resource) from the mid zoom level; when zoomed out
      // further the improvement alone replaces the terrain line.
      const impName = info.improvement ? shortName(info.improvement) + (info.improvement_pillaged ? "(약탈)" : "") : "";
      const imp = [impName, info.resource && (mid || !info.improvement) ? shortName(info.resource) : ""]
        .filter(Boolean).join(" ");
      if (imp && (mid || info.improvement)) lines.push({ text: imp, color: info.improvement_pillaged ? "#ff8a7a" : "#ffd88a", bold: Boolean(info.improvement) });
      lines.push({ yields: YK.filter((k) => info.yields?.[k]).map((k) => ({ k, v: info.yields[k] })) });
    }
    const lh = fs * 1.15;
    let y = p.y - ((lines.length - 1) * lh) / 2;
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const line of lines) {
      if (line.yields) {
        if (!line.yields.length) { y += lh; continue; }
        ctx.font = `bold ${fs}px system-ui`;
        const parts = line.yields.map((e) => ({ ...e, s: num(e.v, 1) }));
        const pre = line.prefix || "";
        const widths = parts.map((e) => ctx.measureText(e.s).width + fs * 0.85);
        const preW = pre ? ctx.measureText(pre).width : 0;
        let x = p.x - (widths.reduce((a, b) => a + b, 0) + preW) / 2;
        if (pre) { ctx.textAlign = "left"; ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.strokeText(pre, x, y); ctx.fillStyle = "#cfe0ea"; ctx.fillText(pre, x, y); x += preW; }
        parts.forEach((e, i) => {
          ctx.beginPath(); ctx.arc(x + fs * 0.3, y, fs * 0.3, 0, Math.PI * 2); ctx.fillStyle = YDOT[e.k]; ctx.fill();
          ctx.lineWidth = 0.8; ctx.strokeStyle = "rgba(0,0,0,0.7)"; ctx.stroke();
          ctx.textAlign = "left"; ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.85)";
          ctx.strokeText(e.s, x + fs * 0.65, y); ctx.fillStyle = "#ffffff"; ctx.fillText(e.s, x + fs * 0.65, y);
          x += widths[i];
        });
        ctx.textAlign = "center";
      } else {
        ctx.font = `${line.bold ? "bold " : ""}${fs}px system-ui`;
        ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.8)"; ctx.strokeText(line.text, p.x, y);
        ctx.fillStyle = line.color || "#fff"; ctx.fillText(line.text, p.x, y);
      }
      y += lh;
    }
  }
  function cityColor(cityId) {
    const index = state.cities.findIndex((c) => c.id === cityId);
    return SERIES[(index < 0 ? 0 : index) % SERIES.length];
  }
  let overlayMax = 1;
  function computeOverlayMax() {
    overlayMax = 1;
    if (YK.includes(S.overlay)) for (const t of S.tiles.values()) overlayMax = Math.max(overlayMax, (t.district ? t.per_citizen?.[S.overlay] : t.yields?.[S.overlay]) || 0);
  }
  function path(ctx, pts) { ctx.beginPath(); pts.forEach((v, i) => (i ? ctx.lineTo(v[0], v[1]) : ctx.moveTo(v[0], v[1]))); ctx.closePath(); }
  function decorateTile(ctx, t, p, size, pts) {
    const info = S.tiles.get(`${t.x},${t.y}`);
    path(ctx, pts);
    if (!info) { ctx.fillStyle = "rgba(6,12,16,0.62)"; ctx.fill(); return; }
    if (YK.includes(S.overlay)) {
      const v = (info.district ? info.per_citizen?.[S.overlay] : info.yields?.[S.overlay]) || 0;
      ctx.fillStyle = "rgba(6,12,16,0.55)"; ctx.fill();
      if (v > 0) { path(ctx, pts); ctx.globalAlpha = 0.25 + 0.65 * Math.min(1, v / overlayMax); ctx.fillStyle = YCOL[S.overlay]; ctx.fill(); ctx.globalAlpha = 1; }
    } else if (S.overlay === "worked") {
      ctx.fillStyle = info.city_center ? "rgba(214,173,89,0.75)" : info.worked ? "rgba(76,191,125,0.72)" : "rgba(80,70,40,0.55)"; ctx.fill();
    } else if (S.overlay === "territory") {
      ctx.globalAlpha = 0.55; ctx.fillStyle = cityColor(info.cityId); ctx.fill(); ctx.globalAlpha = 1;
    } else if (S.overlay === "adjacency") {
      ctx.fillStyle = info.district ? "rgba(57,135,229,0.65)" : "rgba(6,12,16,0.45)"; ctx.fill();
    }
    ctx.strokeStyle = "rgba(0,0,0,0.35)"; ctx.lineWidth = 0.6; ctx.stroke();
  }
  function markTile(ctx, t, p, size, pts) {
    const key = `${t.x},${t.y}`;
    const info = S.tiles.get(key);
    if (!info) return;
    const fontPx = Math.max(7, 8 * state.scale);
    if (S.overlay === "details" && size > 14) drawTileDetails(ctx, info, p, size);
    if (overlayActive() && state.scale > 0.55) {
      let text = null;
      if (YK.includes(S.overlay)) { const v = (info.district ? info.per_citizen?.[S.overlay] : info.yields?.[S.overlay]) || 0; text = v ? num(v, 1) : null; }
      if (S.overlay === "adjacency" && info.adjacency) { const tot = YK.map((k) => info.adjacency[k] ? `${num(info.adjacency[k], 1)}${YL[k][0]}` : "").filter(Boolean).join(" "); text = tot || (info.district ? "0" : null); }
      if (S.overlay === "worked" && info.citizens > 1) text = `×${info.citizens}`;
      if (text) {
        ctx.font = `bold ${fontPx}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.lineWidth = 3; ctx.strokeStyle = "rgba(0,0,0,0.75)"; ctx.strokeText(text, p.x, p.y + size * 0.35);
        ctx.fillStyle = "#ffffff"; ctx.fillText(text, p.x, p.y + size * 0.35);
      }
    }
    if (info.cityId === state.selectedCityId) {
      if (info.pref && state.scale > 0.45) {
        ctx.font = `${fontPx}px system-ui`; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(info.pref.block ? "⛔" : info.pref.lock ? "🔒" : (info.pref.priority > 0 ? "▲" : info.pref.priority < 0 ? "▼" : ""), p.x + size * 0.42, p.y - size * 0.4);
      }
      if (S.citizenEdit && !info.city_center && (info.workable !== false)) {
        path(ctx, pts); ctx.setLineDash([3, 3]); ctx.strokeStyle = info.worked ? "#91f5bc" : "#e3c77e"; ctx.lineWidth = 1.4; ctx.stroke(); ctx.setLineDash([]);
      }
    }
  }
  function afterDraw(ctx) {
    if (!overlayActive() && !S.citizenEdit) return;
    const names = { Food: "식량", Production: "생산력", Gold: "금", Science: "과학", Culture: "문화", Faith: "신앙", worked: "시민 작업 상태 (초록 = 작업 중)", territory: "도시 영토", adjacency: "특수지구 인접 보너스" };
    const text = [overlayActive() ? `표시: ${names[S.overlay]}` : "", S.citizenEdit ? "시민 배치 모드: 선택 도시 타일 클릭 = 배치/해제" : ""].filter(Boolean).join(" · ");
    ctx.font = "12px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
    const w = ctx.measureText(text).width + 16;
    ctx.fillStyle = "rgba(11,20,26,0.85)"; ctx.fillRect(8, 8, w, 22);
    ctx.fillStyle = "#e3e9ec"; ctx.fillText(text, 16, 12);
  }
  function onTileClick(pos) {
    const c = city();
    renderTileDetail();
    if (!S.citizenEdit || !c) { if (S.prod) renderProduction(); return false; }
    const info = S.tiles.get(`${pos.x},${pos.y}`);
    if (!info || info.cityId !== c.id) { status("시민 배치 모드: 선택한 도시가 소유한 타일을 클릭하세요."); return true; }
    if (info.city_center) { status("도심 타일은 항상 작업됩니다."); return true; }
    if (info.district) {
      const next = (info.citizens || 0) < (info.specialist_slots || 0) ? (info.citizens || 0) + 1 : 0;
      void assignTile(pos.x, pos.y, next);
    } else {
      void assignTile(pos.x, pos.y, info.worked ? 0 : 1);
    }
    return true;
  }

  // --------------------------------------------------------------- turns
  async function runTurns(n) {
    const turns = Math.max(1, Math.min(100, Number(n || $("simTurnsInput").value) || 1));
    await guarded(async () => {
      const c = city();
      const r = await api("/api/sim/turn", "POST", { turns, city_id: c?.id || null });
      state.game = r.game_state;
      state.cities = r.cities;
      patchTiles(r.map_changes);
      S.lastReport = r.reports[r.reports.length - 1];
      for (const report of r.reports) for (const e of report.events) S.log.unshift({ turn: report.turn_processed, ...e });
      S.log = S.log.slice(0, 400);
      S.previewHistory = null;
      S.prod = null;
      renderCities();
      if (typeof refreshResearchPanel === "function" && !S.scenarioId) await refreshResearchPanel();
      await loadHistory();
      await refreshAll();
      renderLog();
      const completed = r.reports.flatMap((rep) => rep.events.filter((e) => e.type === "completed").map((e) => label(e.id)));
      const research = r.reports.flatMap((rep) => rep.events.filter((e) => e.phase === 8 && e.type === "completed").map((e) => label(e.id)));
      status([`${turns}턴 진행 → 턴 ${r.turn}`, completed.length ? `완성: ${completed.join(", ")}` : "", research.length ? `연구: ${research.join(", ")}` : ""].filter(Boolean).join(" · "));
    });
  }
  async function previewTurns() {
    const turns = Math.max(1, Math.min(100, Number($("simTurnsInput").value) || 1));
    await guarded(async () => {
      const r = await api("/api/sim/turn/preview", "POST", { turns });
      S.previewHistory = r.history;
      renderHistory();
      showTab("bottom", "history");
      renderChart();
      status(`${turns}턴 미리보기 완료 (저장되지 않음) · 최종 턴 ${r.final_turn}`);
    });
  }
  async function loadHistory() {
    try { S.history = (await api("/api/sim/history")).history || []; } catch (e) { S.history = []; }
    renderHistory(); renderChart();
  }
  function renderHistory() {
    const root = $("simHistory"); if (!root) return;
    const rows = S.previewHistory || S.history;
    const c = city();
    if (!rows.length) { root.innerHTML = '<p class="muted">아직 기록이 없습니다. ‘턴 실행’ 또는 ‘미리보기’를 누르세요.</p>'; return; }
    const cid = c && rows[0].cities[c.id] ? c.id : Object.keys(rows[0].cities)[0];
    root.innerHTML = `<div class="form-row"><b>${S.previewHistory ? "미리보기 결과 (저장 안 됨)" : "턴별 시뮬레이션 기록"}</b><span class="sub">${esc(rows[0].cities[cid]?.name || cid)} · ${rows.length}턴</span>
      ${S.previewHistory ? '<button class="mini" id="hsClear">미리보기 닫기</button>' : ""}</div>
      <table class="grid"><thead><tr><th>턴</th><th class="num">인구</th><th class="num">식량</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}<th>생산</th><th>완성</th><th>연구 완료</th><th>배치</th></tr></thead><tbody>
      ${rows.map((row) => { const d = row.cities[cid]; if (!d) return ""; return `<tr><td>${row.turn}</td><td class="num">${d.population}${d.grew ? " ▲" : ""}</td><td class="num">${num(d.stored_food, 1)}/${num(d.food_required, 0)}</td>${YK.map((k) => `<td class="num">${num(d.yields[k], 1)}</td>`).join("")}
        <td>${esc(label(d.production_item) || "-")} ${d.production_cost ? `${num(d.production_progress, 0)}/${num(d.production_cost, 0)}` : ""}</td><td>${esc((d.completed || []).map(label).join(", "))}</td>
        <td>${esc(row.empire.completed_research.map((x) => label(x.id)).join(", "))}</td><td class="sub">${esc(Object.keys(d.assignment || {}).length)}타일</td></tr>`; }).join("")}
      </tbody></table>`;
    const clear = root.querySelector("#hsClear");
    if (clear) clear.onclick = () => { S.previewHistory = null; renderHistory(); renderChart(); };
  }
  function renderLog() {
    const root = $("simLog"); if (!root) return;
    if (!S.log.length) { root.innerHTML = '<p class="muted">턴을 실행하면 이벤트가 기록됩니다.</p>'; return; }
    const names = Object.fromEntries(state.cities.map((c) => [c.id, c.name]));
    root.innerHTML = `<table class="grid"><thead><tr><th>턴</th><th>단계</th><th>도시</th><th>이벤트</th></tr></thead><tbody>
      ${S.log.map((e) => `<tr><td>${e.turn}</td><td class="sub">${e.phase ?? ""}</td><td>${esc(names[e.city_id] || (e.category ? "제국" : ""))}</td><td>${esc(describeEvent(e))}</td></tr>`).join("")}</tbody></table>`;
  }

  // ------------------------------------------------------------ scenarios
  async function loadScenarios() {
    try { S.scenarios = (await api("/api/scenarios", "GET", undefined, false)).scenarios || []; } catch (e) { S.scenarios = []; }
    const sel = $("scenarioSelect");
    sel.innerHTML = '<option value="">현재 게임</option>' + S.scenarios.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} (턴 ${esc(s.turn)})</option>`).join("");
    sel.value = S.scenarioId || "";
    renderScenarios();
  }
  function renderScenarios() {
    const root = $("simScenarios"); if (!root) return;
    const cities = state.cities;
    root.innerHTML = `
      <div class="block"><h4><span>시나리오 만들기</span></h4>
        <div class="form-row"><input id="scName" placeholder="시나리오 이름" maxlength="80" style="width:200px" />
          <select id="scSource"><option value="current">현재 게임 상태 복제</option>${S.scenarios.map((s) => `<option value="${esc(s.id)}">${esc(s.name)} 복제</option>`).join("")}</select>
          <button class="primary" id="scCreate" type="button">생성</button></div>
        <div class="form-row"><label>전략 일괄 실험</label><select id="scCity">${cities.map((c) => `<option value="${esc(c.id)}" ${c.id === state.selectedCityId ? "selected" : ""}>${esc(c.name)}</option>`).join("")}</select>
          ${S.presets.filter((p) => p.id !== "custom").map((p) => `<label><input type="checkbox" data-strat="${esc(p.id)}" ${["food", "production", "science"].includes(p.id) ? "checked" : ""}/> ${esc(p.label)}</label>`).join("")}
          <label>턴</label><input type="number" id="scTurns" min="1" max="100" value="10" />
          <button id="scBatch" type="button">전략별 시나리오 생성 후 실행</button></div>
        <p class="note">각 시나리오는 현재 상태의 독립 복사본입니다. 한 시나리오의 실행은 다른 시나리오나 현재 게임에 영향을 주지 않습니다.</p>
      </div>
      <div class="block"><h4><span>시나리오 목록</span><span class="form-row" style="margin:0"><label>비교 턴(빈칸 = 저장된 기록)</label><input type="number" id="scCmpTurns" min="1" max="100" style="width:60px" /><button id="scCompare" type="button">선택 비교</button></span></h4>
        ${S.scenarios.length ? `<table class="grid"><thead><tr><th></th><th>이름</th><th class="num">턴</th><th class="num">진행</th><th>도시 설정</th><th></th></tr></thead><tbody>
        ${S.scenarios.map((s) => `<tr><td><input type="checkbox" data-cmp="${esc(s.id)}" ${S.compareSel.has(s.id) ? "checked" : ""}/></td><td><b>${esc(s.name)}</b>${s.id === S.scenarioId ? ' <span class="chip">열림</span>' : ""}<div class="sub">${esc(s.description || s.source || "")}</div></td>
          <td class="num">${esc(s.turn)}</td><td class="num">${esc(s.turns_simulated)}</td><td class="sub">${esc(s.cities.map((c) => `${c.name}: ${presetLabel(c.strategy)}${c.citizen_mode === "auto" ? "(자동)" : ""}`).join(", "))}</td>
          <td><div class="row-actions"><button class="mini" data-sc="open" data-id="${esc(s.id)}">열기</button><button class="mini" data-sc="rename" data-id="${esc(s.id)}">이름</button><button class="mini" data-sc="reset" data-id="${esc(s.id)}">원본 복원</button><button class="mini" data-sc="apply" data-id="${esc(s.id)}">게임에 적용</button><button class="mini" data-sc="delete" data-id="${esc(s.id)}">삭제</button></div></td></tr>`).join("")}
        </tbody></table>` : '<p class="muted">시나리오가 없습니다.</p>'}
      </div>
      ${S.compare ? renderCompare(S.compare) : ""}`;
    root.querySelector("#scCreate").onclick = () => guarded(async () => {
      const name = root.querySelector("#scName").value.trim() || `시나리오 ${S.scenarios.length + 1}`;
      const s = await api("/api/scenarios", "POST", { name, source: root.querySelector("#scSource").value }, false);
      await loadScenarios(); await switchScenario(s.id);
    }, "시나리오를 만들었습니다.");
    root.querySelector("#scBatch").onclick = () => guarded(async () => {
      const strategies = [...root.querySelectorAll("[data-strat]:checked")].map((i) => i.dataset.strat);
      const turns = Number(root.querySelector("#scTurns").value) || 10;
      const r = await api("/api/scenarios/strategy-batch", "POST", { city_id: root.querySelector("#scCity").value, strategies, turns, source: S.scenarioId || "current" }, false);
      S.compare = r.comparison;
      S.compareSel = new Set(r.created);
      S.chartSource = "compare";
      await loadScenarios(); renderChart();
    }, "전략별 시나리오를 실행했습니다.");
    root.querySelectorAll("[data-cmp]").forEach((b) => (b.onchange = () => { b.checked ? S.compareSel.add(b.dataset.cmp) : S.compareSel.delete(b.dataset.cmp); }));
    root.querySelector("#scCompare").onclick = () => guarded(async () => {
      const ids = [...S.compareSel].filter((id) => S.scenarios.some((s) => s.id === id));
      const turns = Number(root.querySelector("#scCmpTurns").value) || null;
      S.compare = await api("/api/scenarios/compare", "POST", { scenario_ids: ids, turns, city_id: state.selectedCityId }, false);
      S.chartSource = "compare"; renderScenarios(); renderChart();
    }, "시나리오를 비교했습니다.");
    root.querySelectorAll("[data-sc]").forEach((b) => (b.onclick = () => scenarioAction(b.dataset.sc, b.dataset.id)));
  }
  function renderCompare(cmp) {
    const rows = cmp.rows;
    if (!rows.length) return "";
    const best = {};
    for (const k of YK) best[k] = Math.max(...rows.map((r) => r.final.yields[k]));
    const cname = cmp.city_id ? (state.cities.find((c) => c.id === cmp.city_id)?.name || cmp.city_id) : "전체 도시";
    return `<div class="block"><h4><span>시나리오 비교 · ${esc(cname)} ${cmp.turns ? `· ${cmp.turns}턴 진행` : "· 저장된 기록"}</span></h4>
      <table class="grid"><thead><tr><th>시나리오</th><th class="num">턴</th><th class="num">최종 인구</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}<th class="num">누적 생산</th><th class="num">누적 과학</th><th class="num">누적 문화</th><th>완성</th><th>연구</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td><b>${esc(r.name)}</b></td><td class="num">${esc(r.start_turn)}→${esc(r.final_turn)}</td><td class="num">${r.final.population}</td>${YK.map((k) => `<td class="num" ${Math.abs(r.final.yields[k] - best[k]) < 1e-9 && rows.length > 1 ? 'style="font-weight:700;color:#fff"' : ""}>${num(r.final.yields[k], 1)}</td>`).join("")}
        <td class="num">${num(r.final.cumulative.Production, 0)}</td><td class="num">${num(r.final.cumulative.Science, 1)}</td><td class="num">${num(r.final.cumulative.Culture, 1)}</td>
        <td class="sub">${esc(r.final.completed.map(label).join(", "))}</td><td class="sub">${esc([...r.final.technologies_completed, ...r.final.civics_completed].map(label).join(", "))}</td></tr>`).join("")}
      </tbody></table><p class="note">굵은 값 = 해당 열 최댓값. 모든 값은 각 시나리오를 독립적으로 시뮬레이션한 실제 계산 결과입니다. 그래프 탭에서 턴별 변화를 볼 수 있습니다.</p></div>`;
  }
  async function scenarioAction(action, id) {
    if (action === "open") return switchScenario(id);
    if (action === "rename") {
      const s = S.scenarios.find((x) => x.id === id);
      const name = prompt("새 이름", s?.name || "");
      if (!name) return;
      return guarded(async () => { await api(`/api/scenarios/${id}`, "PATCH", { name }, false); await loadScenarios(); renderTop(); }, "이름을 바꿨습니다.");
    }
    if (action === "reset") return guarded(async () => { await api(`/api/scenarios/${id}/reset`, "POST", undefined, false); await loadScenarios(); if (S.scenarioId === id) await switchScenario(id); }, "시나리오를 원본 상태로 복원했습니다.");
    if (action === "apply") {
      if (!confirm("이 시나리오 상태로 현재 게임(메모리)을 교체할까요? 디스크 저장은 ‘저장’을 눌러야 합니다.")) return;
      return guarded(async () => { await api(`/api/scenarios/${id}/apply`, "POST", undefined, false); await switchScenario(""); }, "시나리오를 현재 게임에 적용했습니다.");
    }
    if (action === "delete") {
      if (!confirm("시나리오를 삭제할까요?")) return;
      return guarded(async () => { await api(`/api/scenarios/${id}`, "DELETE", undefined, false); S.compareSel.delete(id); if (S.scenarioId === id) await switchScenario(""); await loadScenarios(); }, "시나리오를 삭제했습니다.");
    }
  }
  async function reloadDocs() {
    if (S.scenarioId) {
      const st = await api(`/api/scenarios/${S.scenarioId}/state`, "GET", undefined, false);
      state.map = st.map; state.cities = Object.values(st.cities); state.game = st.game;
    } else {
      const [m, c, g] = await Promise.all([fetch("/api/map", { cache: "no-store" }), fetch("/api/cities", { cache: "no-store" }), fetch("/api/game", { cache: "no-store" })]);
      state.map = await m.json(); state.cities = await c.json(); state.game = await g.json();
    }
    if (typeof clearTileDetailCache === "function") clearTileDetailCache();
  }
  async function switchScenario(id) {
    S.scenarioId = id || null;
    $("scenarioSelect").value = id || "";
    document.body.classList.toggle("scenario-mode", Boolean(id));
    await guarded(async () => {
      await reloadDocs();
      S.prod = null; S.empire = null; S.optimize = null; S.previewHistory = null; S.lastReport = null;
      if (!state.cities.some((c) => c.id === state.selectedCityId)) state.selectedCityId = state.cities[0]?.id || null;
      renderCities();
      await loadHistory();
      await refreshAll();
      if (document.querySelector('[data-tabs="right"] .tab.active')?.dataset.tab === "empire") await loadEmpire();
      renderScenarios();
    }, id ? "시나리오를 열었습니다. 현재 게임은 변경되지 않습니다." : "현재 게임으로 돌아왔습니다.");
  }

  // --------------------------------------------------------------- chart
  function chartSeries() {
    const metric = S.chartMetric;
    const pick = (row, cid) => {
      const cities = cid ? [row.cities[cid]].filter(Boolean) : Object.values(row.cities);
      return metric === "population" ? cities.reduce((a, c) => a + c.population, 0) : cities.reduce((a, c) => a + (c.yields?.[metric] || 0), 0);
    };
    if (S.chartSource === "compare" && S.compare) {
      return S.compare.rows.map((r) => {
        const index = S.scenarios.findIndex((s) => s.id === r.scenario_id);
        return { name: r.name, color: SERIES[(index < 0 ? 0 : index) % SERIES.length],
          points: r.series.map((p) => ({ x: p.turn, y: metric === "population" ? p.population : p.yields[metric] })) };
      });
    }
    const rows = S.previewHistory || S.history;
    const cid = state.selectedCityId && rows[0]?.cities?.[state.selectedCityId] ? state.selectedCityId : null;
    return rows.length ? [{ name: (cid ? rows[0].cities[cid].name : "전체 도시") + (S.previewHistory ? " (미리보기)" : ""), color: SERIES[0], points: rows.map((row) => ({ x: row.turn, y: pick(row, cid) })) }] : [];
  }
  function renderChart() {
    const root = $("simChart"); if (!root) return;
    const series = chartSeries().slice(0, 6);
    const metricName = S.chartMetric === "population" ? "인구" : YL[S.chartMetric];
    root.innerHTML = `<div class="form-row"><label>지표</label><select id="chMetric"><option value="population">인구</option>${YK.map((k) => `<option value="${k}">${YL[k]}</option>`).join("")}</select>
      <label>데이터</label><select id="chSource"><option value="history">턴 기록 / 미리보기</option><option value="compare" ${S.compare ? "" : "disabled"}>시나리오 비교</option></select></div>
      <div id="chBody"></div>`;
    root.querySelector("#chMetric").value = S.chartMetric;
    root.querySelector("#chSource").value = S.chartSource === "compare" && S.compare ? "compare" : "history";
    root.querySelector("#chMetric").onchange = (e) => { S.chartMetric = e.target.value; renderChart(); };
    root.querySelector("#chSource").onchange = (e) => { S.chartSource = e.target.value; renderChart(); };
    const body = root.querySelector("#chBody");
    if (!series.length || !series.some((s) => s.points.length)) { body.innerHTML = '<p class="muted">표시할 기록이 없습니다. 턴을 실행하거나 시나리오를 비교하세요.</p>'; return; }
    lineChart(body, series, `${metricName} — 턴별 변화`);
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    const e = Math.pow(10, Math.floor(Math.log10(v)));
    const f = v / e;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * e;
  }
  function endLabels(series, sx, sy) {
    // Direct end labels, nudged apart so overlapping series stay readable.
    const items = series.map((s) => {
      const p = s.points[s.points.length - 1];
      if (!p) return null;
      const name = s.name.split(" · ").pop();
      return { x: sx(p.x) + 8, y: sy(p.y) + 4, text: `${name.length > 12 ? name.slice(0, 11) + "…" : name} ${num(p.y, 1)}` };
    }).filter(Boolean).sort((a, b) => a.y - b.y);
    for (let i = 1; i < items.length; i++) if (items[i].y - items[i - 1].y < 13) items[i].y = items[i - 1].y + 13;
    return items;
  }
  function lineChart(container, series, title) {
    const W = Math.max(420, container.clientWidth || 640), H = 230, m = { l: 44, r: 120, t: 10, b: 26 };
    const xs = series.flatMap((s) => s.points.map((p) => p.x)), ys = series.flatMap((s) => s.points.map((p) => p.y));
    const x0 = Math.min(...xs), x1 = Math.max(...xs, x0 + 1);
    const yMin = Math.min(0, ...ys), yMax = niceMax(Math.max(...ys) * 1.05);
    const sx = (x) => m.l + ((x - x0) / (x1 - x0)) * (W - m.l - m.r);
    const sy = (y) => H - m.b - ((y - yMin) / (yMax - yMin || 1)) * (H - m.t - m.b);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => yMin + f * (yMax - yMin));
    const xticks = [...new Set(xs)].sort((a, b) => a - b);
    const step = Math.max(1, Math.ceil(xticks.length / 10));
    const svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
      ${ticks.map((t) => `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(t)}" y2="${sy(t)}" stroke="#24384a" stroke-width="1"/><text x="${m.l - 6}" y="${sy(t) + 4}" fill="#7f95a2" font-size="11" text-anchor="end">${num(t, t < 10 ? 1 : 0)}</text>`).join("")}
      ${xticks.filter((_, i) => i % step === 0).map((x) => `<text x="${sx(x)}" y="${H - 8}" fill="#7f95a2" font-size="11" text-anchor="middle">${x}</text>`).join("")}
      ${series.map((s) => `<polyline fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round" points="${s.points.map((p) => `${sx(p.x)},${sy(p.y)}`).join(" ")}"/>`).join("")}
      ${series.map((s) => { const p = s.points[s.points.length - 1]; return p ? `<circle cx="${sx(p.x)}" cy="${sy(p.y)}" r="4" fill="${s.color}" stroke="#131f29" stroke-width="2"/>` : ""; }).join("")}
      ${series.length <= 4 ? endLabels(series, sx, sy).map((l) => `<text x="${l.x}" y="${l.y}" fill="#dbe6ea" font-size="11">${esc(l.text)}</text>`).join("") : ""}
      <line id="chCross" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="#7f95a2" stroke-width="1" visibility="hidden"/>
      <rect id="chHit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`;
    container.innerHTML = `<div class="sub">${esc(title)}</div>${series.length >= 2 ? `<div class="chart-legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : ""}
      <div class="chart-wrap">${svg}<div class="chart-tip"></div></div>
      <details><summary class="sub">표로 보기</summary><table class="grid"><thead><tr><th>턴</th>${series.map((s) => `<th class="num">${esc(s.name)}</th>`).join("")}</tr></thead><tbody>
      ${xticks.map((x) => `<tr><td>${x}</td>${series.map((s) => `<td class="num">${num(s.points.find((p) => p.x === x)?.y, 2)}</td>`).join("")}</tr>`).join("")}</tbody></table></details>`;
    const wrap = container.querySelector(".chart-wrap"), tip = wrap.querySelector(".chart-tip");
    const svgEl = wrap.querySelector("svg"), hit = svgEl.querySelector("#chHit"), cross = svgEl.querySelector("#chCross");
    hit.addEventListener("mousemove", (ev) => {
      const box = svgEl.getBoundingClientRect();
      const px = ((ev.clientX - box.left) / box.width) * W;
      const xv = xticks.reduce((best, x) => (Math.abs(sx(x) - px) < Math.abs(sx(best) - px) ? x : best), xticks[0]);
      cross.setAttribute("x1", sx(xv)); cross.setAttribute("x2", sx(xv)); cross.setAttribute("visibility", "visible");
      tip.style.display = "block";
      tip.style.left = (sx(xv) / W) * box.width + "px";
      tip.style.top = (ev.clientY - box.top) + "px";
      tip.innerHTML = `<div><b>턴 ${xv}</b></div>` + series.map((s) => `<div class="r"><i style="background:${s.color}"></i>${esc(s.name)} <b>${num(s.points.find((p) => p.x === xv)?.y, 2)}</b></div>`).join("");
    });
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
  }

  // ------------------------------------------------------------ ledger
  async function loadLedger() {
    const c = city();
    if (!c) { S.ledger = null; renderLedger(); return; }
    try {
      S.ledger = await api(`/api/sim/cities/${encodeURIComponent(c.id)}/ledger`); S.ledgerCity = c.id;
    } catch (error) { S.ledger = null; status("장부 오류: " + error.message); }
    renderLedger();
  }
  // Every city, one yield: section totals per city, lines on demand.
  async function loadYieldLedger() {
    const key = S.yieldLedgerKey;
    const root = $("simYieldLedger");
    if (root && !S.yieldLedger) root.innerHTML = '<p class="muted">계산 중…</p>';
    try {
      const data = await api(`/api/sim/ledger/yield/${encodeURIComponent(key)}`);
      if (key !== S.yieldLedgerKey) return;
      S.yieldLedger = data; S.yieldLedgerStale = false;
    } catch (error) { status("자원별 장부 오류: " + error.message); }
    renderYieldLedger();
  }
  function renderYieldLedger() {
    const root = $("simYieldLedger"); if (!root) return;
    const L = S.yieldLedger;
    const picker = `<span class="yl-pick">${["Science", "Culture", "Gold", "Faith", "Food", "Production"].map((k) =>
      `<button type="button" class="mini${k === S.yieldLedgerKey ? " on" : ""}" data-yl="${k}">${YL[k]}</button>`).join("")}</span>`;
    if (!L || L.yield !== S.yieldLedgerKey) {
      root.innerHTML = `<div class="ledger-head"><b>자원별 장부</b> ${picker}</div><p class="muted">계산 중…</p>`;
      bindYieldPicker(root); return;
    }
    const cols = L.columns;
    const cell = (v) => `<td class="num${Math.abs(v || 0) < 1e-9 ? " zero" : ""}">${Math.abs(v || 0) < 1e-9 ? "·" : num(v, 1)}</td>`;
    const ordered = [...L.cities].sort((a, b) => b.total - a.total);
    let rows = "";
    for (const c of ordered) {
      const open = S.yieldLedgerOpen.has(c.city_id);
      rows += `<tr class="yl-city" data-city="${esc(c.city_id)}"><td>${open ? "▾" : "▸"} <a href="#" data-pick="${esc(c.city_id)}">${esc(c.name)}</a> <span class="sub">인구 ${esc(c.population)}</span>${c.reconciled ? "" : ' <span class="badge s-unsupported">검산 불일치</span>'}</td>${cols.map((col) => cell(c.sections[col.id])).join("")}<td class="num"><b>${num(c.total, 1)}</b></td></tr>`;
      if (open) {
        let last = null;
        for (const line of c.lines) {
          if (line.section !== last) {
            rows += `<tr class="lg-sub"><td colspan="${cols.length + 2}">&nbsp;&nbsp;${esc((cols.find((x) => x.id === line.section) || {}).label || line.section)}</td></tr>`;
            last = line.section;
          }
          rows += `<tr class="yl-line"><td colspan="${cols.length + 1}">&nbsp;&nbsp;&nbsp;&nbsp;${esc(line.label)}${line.location ? ` <span class="sub">(${esc(line.location)})</span>` : ""}<div class="sub formula">${esc(line.formula)}</div></td><td class="num">${sgn(line.value, 2)}</td></tr>`;
        }
      }
    }
    rows += `<tr class="lg-total"><td>전체 도시 합계</td>${cols.map((col) => cell(L.section_totals[col.id])).join("")}<td class="num"><b>${num(L.total, 1)}</b></td></tr>`;
    root.innerHTML = `<div class="ledger-head"><b>${esc(L.yield_label)} 장부 · 모든 도시 · 턴 ${esc(L.turn)}</b> ${picker}
        <button type="button" class="mini" id="ylAll">모두 펼치기</button><button type="button" class="mini" id="ylNone">모두 접기</button>
        <span class="sub">도시 줄을 누르면 그 도시의 ${esc(L.yield_label)} 항목이 펼쳐집니다. 도시 이름을 누르면 그 도시를 선택합니다. 제국 전체 수입·유지비는 포함하지 않은 도시 산출 합계입니다.</span></div>
      <table class="grid ledger"><thead><tr><th>도시</th>${cols.map((col) => `<th class="num" title="${esc(col.label)}">${esc(col.label.split(" (")[0])}</th>`).join("")}<th class="num">합계</th></tr></thead><tbody>${rows}</tbody></table>`;
    bindYieldPicker(root);
    root.querySelector("#ylAll").onclick = () => { S.yieldLedgerOpen = new Set(L.cities.map((c) => c.city_id)); renderYieldLedger(); };
    root.querySelector("#ylNone").onclick = () => { S.yieldLedgerOpen = new Set(); renderYieldLedger(); };
    root.querySelectorAll("tr.yl-city").forEach((tr) => (tr.onclick = (event) => {
      const pick = event.target.closest("[data-pick]");
      if (pick) { event.preventDefault(); if (typeof window.selectCity === "function") window.selectCity(pick.dataset.pick); return; }
      const id = tr.dataset.city;
      if (S.yieldLedgerOpen.has(id)) S.yieldLedgerOpen.delete(id); else S.yieldLedgerOpen.add(id);
      renderYieldLedger();
    }));
  }
  function bindYieldPicker(root) {
    root.querySelectorAll("[data-yl]").forEach((b) => (b.onclick = () => {
      if (S.yieldLedgerKey === b.dataset.yl) return;
      S.yieldLedgerKey = b.dataset.yl; S.yieldLedger = null; renderYieldLedger(); void loadYieldLedger();
    }));
  }
  function renderTradeLedger(L) {
    const routes = L.trade_routes || [];
    if (!routes.length) return `<div class="block" style="margin-top:10px"><h4><span>교역로 장부</span></h4><p class="sub">이 도시에서 출발하는 교역로가 없습니다 (개요 탭에서 추가).</p></div>`;
    const YK = ["Food", "Production", "Gold", "Science", "Culture", "Faith"];
    const YLb = { Food: "식량", Production: "생산", Gold: "금", Science: "과학", Culture: "문화", Faith: "신앙" };
    const cell = (y, k) => `<td class="num${y && y[k] ? "" : " zero"}">${y && y[k] ? num(y[k], 1) : ""}</td>`;
    return `<div class="block" style="margin-top:10px"><h4><span>교역로 장부 ${routes.length}개</span></h4>
      <table class="grid ledger"><thead><tr><th>교역로 · 근거</th><th>출처</th>${YK.map((k) => `<th class="num">${YLb[k]}</th>`).join("")}</tr></thead><tbody>
      ${routes.map((r) => `<tr class="group"><td><b>${esc(r.label)}</b><div class="sub">${r.timing ? esc(r.timing.formula) + (r.timing.turns_left != null ? ` · 남은 ${r.timing.turns_left}턴` : "") : ""}</div></td><td class="sub">${r.timing ? esc(r.timing.source) : ""}</td>${YK.map((k) => cell(r.yields, k)).join("")}</tr>
        ${r.lines.map((l) => `<tr><td>&nbsp;&nbsp;${esc(l.label)}${l.calc ? ` <span class="sub">${esc(l.calc)}</span>` : ""}</td><td class="sub">${esc(srcText(l.source))}</td>${YK.map((k) => cell(l.yields, k)).join("")}</tr>`).join("")}`).join("")}
      </tbody></table></div>`;
  }
  function srcText(src) {
    if (!src) return "";
    if (src.origin === "Data.zip") return `Data.zip · ${src.file || "?"} · ${src.table || ""}`;
    return `${src.origin}${src.key ? " · " + src.key : ""}`;
  }
  function rawRows(rows) {
    return `<div class="raw-rows">${(rows || []).map((r) => `<code>${esc(Object.entries(r).map(([k, v]) => `${k}=${v}`).join(" "))}</code>`).join("")}</div>`;
  }
  function reqTrace(set, title) {
    if (!set || !set.set) return `<div>${esc(title)}: 조건 없음 (항상 적용)</div>`;
    const mark = (m) => (m === true ? '<span class="req-ok">✓</span>' : m === false ? '<span class="req-no">✕</span>' : '<span class="req-unk">?</span>');
    return `<div>${esc(title)}: <b>${esc(set.set)}</b> (${esc(set.type === "REQUIREMENTSET_TEST_ANY" ? "하나라도 충족" : "모두 충족")}) ${mark(set.met)} · 대상 ${esc(set.subject)}</div>
      <ul class="reasons">${(set.requirements || []).map((r) => `<li>${mark(r.met)} ${esc(r.id)} — ${esc(r.type)}${r.inverse ? " (반전)" : ""} ${esc(Object.entries(r.args || {}).map(([k, v]) => `${k}=${v}`).join(", "))} · ${esc(r.reason || "")}</li>`).join("")}</ul>`;
  }
  function ledgerDetail(line) {
    const src = line.source || {};
    const parts = [`<div><b>출처</b> ${esc(src.origin)}${src.file ? ` · 파일 ${esc(src.file)}` : ""}${src.table ? ` · 테이블 ${esc(src.table)}` : ""}${src.key ? ` · 키 ${esc(src.key)}` : ""}</div>`];
    if (src.note) parts.push(`<div class="sub">${esc(src.note)}</div>`);
    if (src.rows && src.rows.length) parts.push(`<div>원본 XML 행</div>${rawRows(src.rows)}`);
    const m = src.modifier;
    if (m) {
      parts.push(`<div><b>Modifier 경로</b> ${esc(m.source)} → ${esc(m.link.table)} ${m.link.file ? `(${esc(m.link.file)})` : ""} → ${esc(m.chain.join(" → "))}</div>`);
      if (m.link.rows.length) parts.push(rawRows(m.link.rows));
      parts.push(`<div>ModifierType ${esc(m.modifier_type)} · 대상 범위 ${esc(m.collection)} · 효과 ${esc(m.effect)}</div>`);
      if (m.arguments.length) parts.push(`<div>인자</div>${rawRows(m.arguments)}`);
      if (m.requirements && (m.requirements.owner || m.requirements.subject)) {
        parts.push(reqTrace(m.requirements.owner, "소유자 조건"));
        parts.push(reqTrace(m.requirements.subject, "대상 조건"));
      }
    }
    parts.push(`<div class="sub">이 줄까지 누계: ${YK.map((k) => `${YL[k]} ${num(line.running[k], 1)}`).join(" · ")}</div>`);
    return parts.join("");
  }
  function renderLedger() {
    const root = $("simLedger"); if (!root) return;
    const L = S.ledger;
    if (!L) { root.innerHTML = '<p class="muted">도시를 선택하면 산출 장부를 계산합니다.</p>'; return; }
    const fy = S.ledgerYield;
    const keys = fy === "all" ? YK : [fy];
    const cell = (v) => `<td class="num ${Math.abs(v) < 1e-9 ? "zero" : ""}">${Math.abs(v) < 1e-9 ? "·" : sgn(v, 2)}</td>`;
    const ok = L.reconciliation.ok;
    let rows = "", lastSection = null, lastGroup = null;
    const sectionTotal = (sid) => (L.sections.find((x) => x.id === sid) || {}).yields || {};
    const lines = L.lines.filter((l) => fy === "all" || Math.abs(l.yields[fy]) > 1e-9);
    const colspan = 3 + keys.length + (fy === "all" ? 0 : 1);
    lines.forEach((line, i) => {
      if (line.section !== lastSection) {
        if (lastSection === null || line.section === "percent") { /* noop */ }
        if (line.section === "percent") rows += `<tr class="lg-subtotal"><td></td><td colspan="2">소계 (% 보정 전)</td>${keys.map((k) => `<td class="num">${num(L.subtotal[k], 1)}</td>`).join("")}${fy === "all" ? "" : "<td></td>"}</tr>`;
        rows += `<tr class="lg-group"><td colspan="${colspan}">${esc(line.section_label)} <span class="sub">${keys.map((k) => `${YL[k]} ${sgn(sectionTotal(line.section)[k] || 0, 2)}`).join(" · ")}</span></td></tr>`;
        lastSection = line.section; lastGroup = null;
      }
      if (line.group !== lastGroup && line.group !== line.label) {
        rows += `<tr class="lg-sub"><td></td><td colspan="${colspan - 1}">${esc(line.group)}${line.location ? ` <span class="sub">(${esc(line.location)})</span>` : ""}</td></tr>`;
        lastGroup = line.group;
      }
      rows += `<tr class="lg-line" data-i="${L.lines.indexOf(line)}"><td class="sub">${line.no}</td><td>${esc(line.label)}<div class="sub formula">${esc(line.formula)}</div></td><td class="src">${esc(srcText(line.source))}</td>${keys.map((k) => cell(line.yields[k])).join("")}${fy === "all" ? "" : `<td class="num sub">${num(line.running[fy], 1)}</td>`}</tr>`;
      if (i === lines.length - 1 && line.section !== "percent") rows += `<tr class="lg-subtotal"><td></td><td colspan="2">소계 (% 보정 전)</td>${keys.map((k) => `<td class="num">${num(L.subtotal[k], 1)}</td>`).join("")}${fy === "all" ? "" : "<td></td>"}</tr>`;
    });
    rows += `<tr class="lg-total"><td></td><td colspan="2">합계 = 도시 화면 산출량</td>${keys.map((k) => `<td class="num">${num(L.totals[k], 1)}</td>`).join("")}${fy === "all" ? "" : "<td></td>"}</tr>`;
    const growth = L.growth.map((g) => `<tr><td>${esc(g.label)}</td><td class="num">${g.value === null || g.value === undefined ? "–" : num(g.value, 1)}${g.unit ? " " + esc(g.unit) : ""}</td><td class="sub">${esc(g.formula)}</td><td class="sub">${esc(g.source || "")}</td></tr>`).join("");
    const housing = (L.housing.components || []).map((h) => `<tr><td>${esc(h.label)}</td><td class="num">${sgn(h.amount, 2)}</td><td class="sub">${esc(h.source)}</td></tr>`).join("");
    const amen = (L.amenities.components || []).map((h) => `<tr><td>${esc(h.label)}</td><td class="num">${sgn(h.amount, 0)}</td><td class="sub">${esc(h.source)}</td></tr>`).join("");
    const pc = L.production.current;
    root.innerHTML = `
      <div class="ledger-head"><b>${esc(L.name)} 산출 장부 · 턴 ${esc(L.turn)}</b>
        ${ok ? '<span class="badge s-verified">검산 일치</span>' : '<span class="badge s-unsupported">검산 불일치</span>'}
        <span class="sub">장부 줄 합계 = 도시 화면 합계 · 기본 산출 출처: ${esc(L.data_origin.base_yields)}</span>
        <label>산출 <select id="lgYield"><option value="all">전체</option>${YK.map((k) => `<option value="${k}">${YL[k]}만 (누계 포함)</option>`).join("")}</select></label>
        <label><input type="checkbox" id="lgExcl" ${S.ledgerExcluded ? "checked" : ""}/> 제외 항목 표시</label>
        <a href="${esc(q(`/api/sim/cities/${encodeURIComponent(L.city_id)}/ledger.csv`))}" download>CSV 내려받기</a>
        <span class="sub">줄을 누르면 원본 XML 행·Modifier 조건 평가가 펼쳐집니다.</span></div>
      <div class="ledger-sections">${L.sections.map((x) => `<span class="sec"><b>${esc(x.label)}</b> ${x.lines}줄 · ${keys.map((k) => `${YL[k]} ${sgn(x.yields[k], 2)}`).join(" ")}</span>`).join("")}</div>
      <table class="grid ledger"><thead><tr><th>#</th><th>항목 · 계산식</th><th>출처</th>${keys.map((k) => `<th class="num">${YL[k]}</th>`).join("")}${fy === "all" ? "" : `<th class="num">누계</th>`}</tr></thead><tbody>${rows}</tbody></table>
      ${S.ledgerExcluded ? `<div class="block" style="margin-top:10px"><h4><span>합계에 포함되지 않은 항목 ${L.excluded.length}건</span></h4>
        <table class="grid"><thead><tr><th>분류</th><th>위치</th><th>항목</th><th>사유</th>${YK.map((k) => `<th class="num">${YL[k]}</th>`).join("")}</tr></thead><tbody>
        ${L.excluded.map((e) => `<tr><td class="sub">${esc(e.category)}</td><td>${esc(e.location || "")}</td><td>${esc(e.label)}${e.key ? ` <span class="sub">${esc(e.table || "")} ${esc(e.key)}</span>` : ""}</td><td class="sub">${esc(e.reason || "")}</td>${YK.map((k) => `<td class="num zero">${e.yields && e.yields[k] ? num(e.yields[k], 1) : ""}</td>`).join("")}</tr>`).join("")}
        </tbody></table></div>` : ""}
      ${renderTradeLedger(L)}
      <div class="ledger-grid">
        <div class="block"><h4><span>성장 장부</span></h4><table class="grid"><tbody>${growth}</tbody></table></div>
        <div class="block"><h4><span>주거공간 ${num(L.housing.value, 1)} (${L.housing.mode === "manual" ? "수동" : "계산"})</span></h4><table class="grid"><tbody>${housing}</tbody></table>
          <h4 style="margin-top:8px"><span>쾌적도 ${sgn(L.amenities.score, 0)} · ${esc(L.amenities.happiness || "")}</span></h4><table class="grid"><tbody>${amen}
          <tr><td>요구량</td><td class="num">−${esc(L.amenities.required)}</td><td class="sub">${esc(L.amenities.required_formula)}</td></tr></tbody></table></div>
        <div class="block"><h4><span>생산 장부</span></h4>
          ${pc ? `<div class="kv"><span>항목</span><span>${esc(label(pc.target_id))}</span><span>투입 생산력</span><span>${esc(pc.formula)}</span>
            <span>비용</span><span>${(pc.cost_steps || []).map((st) => esc(st.note) + (st.value !== undefined ? ` = ${num(st.value, 1)}` : "")).join(" → ")}</span>
            <span>보너스</span><span>${pc.bonus.length ? pc.bonus.map((b) => `${esc(b.label)} ${sgn(b.percent, 0)}%`).join(", ") : "없음"}</span>
            <span>진행</span><span>${num(pc.progress, 1)} / ${num(pc.cost, 1)} · 남은 ${num(pc.remaining, 1)} · ${pc.eta ? pc.eta + "턴" : "–"}</span>
            <span>초과분 정책</span><span>${esc(L.production.overflow_policy)}</span></div>` : `<p class="muted">생산 대기열이 비어 있습니다. 생산력 ${num(L.production.per_turn, 1)}는 사용되지 않습니다.</p>`}
        </div>
      </div>
      <ul class="reasons">${L.assumptions.map((a) => `<li class="sub">${esc(a)}</li>`).join("")}</ul>`;
    root.querySelector("#lgYield").value = fy;
    root.querySelector("#lgYield").onchange = (e) => { S.ledgerYield = e.target.value; renderLedger(); };
    root.querySelector("#lgExcl").onchange = (e) => { S.ledgerExcluded = e.target.checked; renderLedger(); };
    root.querySelectorAll("tr.lg-line").forEach((tr) => (tr.onclick = () => {
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("lg-detail")) { next.remove(); return; }
      const line = L.lines[Number(tr.dataset.i)];
      const detail = document.createElement("tr");
      detail.className = "lg-detail";
      detail.innerHTML = `<td></td><td colspan="${colspan - 1}">${ledgerDetail(line)}</td>`;
      tr.after(detail);
      if (line.location) {
        const [x, y] = line.location.split(",").map(Number);
        state.selectedTile = { x, y }; drawMap(); renderTileDetail();
      }
    }));
  }

  // ------------------------------------------------------ basis & data
  async function loadBasis() {
    try {
      const [coverage, mods, validation] = await Promise.all([
        api("/api/validation/coverage", "GET", undefined, false), api("/api/sim/modifiers"), api("/api/validation/cases", "GET", undefined, false),
      ]);
      S.coverage = coverage; S.modAudit = mods; S.validation = validation;
    } catch (error) { status("근거 정보 오류: " + error.message); }
    renderBasis();
  }
  function renderBasis() {
    const root = $("simBasis"); if (!root || !S.coverage) return;
    const skippedBy = {};
    for (const s of S.modAudit?.skipped || []) (skippedBy[s.category] = skippedBy[s.category] || []).push(s);
    const catName = { economy_unsupported: "경제 관련 미지원 효과", out_of_scope: "도시 경제 외 효과 (전투·외교 등)", requirement_unsupported: "조건 평가 불가", collection_unsupported: "대상 컬렉션 미지원", one_shot: "1회성 효과", missing: "데이터 누락", data: "데이터 없음" };
    const c = city();
    root.innerHTML = `
      <div class="block"><h4><span>계산 항목별 정확도 상태</span><span class="sub">게임 대조 사례 ${S.coverage.validation.cases}건 · 일치 ${S.coverage.validation.passed}건</span></h4>
        <table class="grid"><thead><tr><th>항목</th><th>상태</th><th>근거 / 한계</th></tr></thead><tbody>
        ${S.coverage.items.map((i) => `<tr><td>${esc(i.label)}</td><td>${badge(i.status)}</td><td class="sub">${esc(i.basis)}${i.evidence ? ` · ${esc(i.evidence)}` : ""}</td></tr>`).join("")}</tbody></table>
        <p class="note">‘검증 완료’는 실제 게임에서 확인한 값이 아래 대조 사례로 일치할 때만 사용합니다. 현재 기본값은 게임 대조 전이므로 어떤 항목도 검증 완료로 표시하지 않습니다.</p></div>
      <div class="block"><h4><span>Modifier 감사 로그</span><span class="sub">적용 ${S.modAudit?.summary?.applied_effects ?? 0} · 건너뜀 ${S.modAudit?.summary?.skipped ?? 0} · 조건 미충족 ${S.modAudit?.summary?.inactive_conditions ?? 0}</span></h4>
        <details><summary class="sub">적용된 효과 ${S.modAudit?.applied?.length ?? 0}건</summary><table class="grid"><tbody>${(S.modAudit?.applied || []).map((a) => `<tr><td>${esc(a.source_kind)} ${esc(label(a.source_id))}</td><td class="sub">${esc(a.modifier_id)}</td><td class="sub">${esc(a.effect)}</td><td class="sub">${esc(a.subject)}</td></tr>`).join("")}</tbody></table></details>
        ${Object.entries(skippedBy).map(([cat, list]) => `<details><summary class="sub">${esc(catName[cat] || cat)} ${list.length}건</summary><table class="grid"><tbody>${list.map((s) => `<tr><td>${esc(s.source_kind)} ${esc(label(s.source_id))}</td><td class="sub">${esc(s.modifier_id)}</td><td class="sub">${esc(s.reason)}</td></tr>`).join("")}</tbody></table></details>`).join("")}
        <details><summary class="sub">조건 미충족(비활성) ${S.modAudit?.inactive?.length ?? 0}건</summary><table class="grid"><tbody>${(S.modAudit?.inactive || []).map((s) => `<tr><td>${esc(s.source_kind)} ${esc(label(s.source_id))}</td><td class="sub">${esc(s.modifier_id)}</td><td class="sub">${esc(s.reason)} · ${esc(s.subject)}</td></tr>`).join("")}</tbody></table></details>
      </div>
      <div class="block"><h4><span>게임 대조 검증 사례</span></h4>
        <p class="note">실제 Civ VI 도시 화면에서 같은 조건(지형·시민 배치·건물·정책)을 만든 뒤 보이는 값을 입력하면, 엔진 계산과 비교해 일치 여부를 기록합니다.</p>
        ${c ? `<div class="form-row"><b>${esc(c.name)}</b> ${YK.map((k) => `<label>${YL[k]}</label><input type="number" step="0.1" data-exp="${k}" style="width:56px" />`).join("")}
          <label>인구</label><input type="number" data-exp="population" style="width:48px" /><label>주거</label><input type="number" step="0.5" data-exp="housing" style="width:48px" />
          <input id="vcName" placeholder="사례 이름" style="width:120px" /><button id="vcAdd" type="button">사례 저장</button></div>` : '<p class="muted">도시를 선택하세요.</p>'}
        <table class="grid"><thead><tr><th>사례</th><th>대상</th><th>상태</th><th>항목 (게임 / 엔진 / 차이)</th><th></th></tr></thead><tbody>
        ${(S.validation?.results || []).map((r) => `<tr><td>${esc(r.name)}</td><td class="sub">${esc(r.city_id)}${r.scenario_id ? " · " + esc(r.scenario_id) : ""}</td><td>${r.status === "일치" ? badge("검증 완료") : `<span class="badge s-unsupported">${esc(r.status)}</span>`}</td>
          <td class="sub">${(r.fields || []).map((f) => `${esc(f.field)} ${num(f.expected, 2)} / ${num(f.actual, 2)} / ${f.difference === null ? "–" : sgn(f.difference, 2)}${f.match ? " ✓" : " ✕"}`).join(" · ")}</td><td><button class="mini" data-vdel="${esc(r.id)}">✕</button></td></tr>`).join("")}
        </tbody></table></div>`;
    const add = root.querySelector("#vcAdd");
    if (add) add.onclick = () => guarded(async () => {
      const expected = {};
      root.querySelectorAll("[data-exp]").forEach((i) => { if (i.value !== "") expected[i.dataset.exp] = Number(i.value); });
      await api("/api/validation/cases", "POST", { city_id: c.id, scenario_id: S.scenarioId, expected, name: root.querySelector("#vcName").value || null }, false);
      await loadBasis();
    }, "대조 사례를 저장했습니다.");
    root.querySelectorAll("[data-vdel]").forEach((b) => (b.onclick = () => guarded(async () => { await api(`/api/validation/cases/${b.dataset.vdel}`, "DELETE", undefined, false); await loadBasis(); })));
  }
  async function loadAudit() {
    try { S.audit = await api("/api/data/audit", "GET", undefined, false); } catch (error) { status("데이터 감사 오류: " + error.message); return; }
    const a = S.audit, x = a.xml_vs_catalog || {};
    $("simData").innerHTML = `
      <div class="block"><h4><span>원본 XML ↔ 카탈로그 일관성</span></h4>
        <div class="kv"><span>rules_db.json</span><span>${a.rules_db.available ? `테이블 ${a.rules_db.stats?.tables} · 행 ${a.rules_db.stats?.rows} · 파일 ${a.rules_db.stats?.files} · ${esc(a.rules_db.generated_at || "")}` : "없음 — tools/build_rules_db.py 실행 필요"}</span>
        <span>catalog.json</span><span>누락 테이블 ${x.summary?.missing_tables ?? "-"} · 빈 행 테이블 ${x.summary?.empty_row_tables ?? "-"} · 행 수 불일치 ${x.summary?.row_count_mismatches ?? "-"} · 산출 값 불일치 ${x.summary?.value_mismatches ?? "-"}</span></div>
        <ul class="reasons">${a.known_discrepancies.map((d) => `<li>${esc(d)}</li>`).join("")}</ul></div>
      <div class="block"><h4><span>엔티티 비교</span></h4><table class="grid"><thead><tr><th>엔티티</th><th class="num">catalog</th><th class="num">XML</th><th>catalog 누락</th></tr></thead><tbody>
        ${a.entities.map((e) => `<tr><td>${esc(e.entity)}</td><td class="num">${e.catalog}</td><td class="num">${e.xml}</td><td class="sub">${esc(e.missing_in_catalog.slice(0, 8).join(", "))}${e.missing_in_catalog.length > 8 ? " …" : ""}</td></tr>`).join("")}</tbody></table>
        <div class="sub">선행 조건 깨진 링크 ${a.broken_prerequisites.length}건 · 없는 Modifier 참조 ${a.modifier_links.missing_modifier_rows.length}건 · 없는 RequirementSet 참조 ${a.modifier_links.missing_requirement_sets.length}건</div></div>
      <div class="block"><h4><span>Modifier 연결 지원 범위</span><span class="sub">지원 ${a.modifier_links.totals.supported} · 경제 미지원 ${a.modifier_links.totals.economy_unsupported} · 범위 밖 ${a.modifier_links.totals.out_of_scope}</span></h4>
        <table class="grid"><thead><tr><th>연결 테이블</th><th class="num">링크</th><th class="num">지원</th><th class="num">경제 미지원</th><th class="num">범위 밖</th><th class="num">누락</th></tr></thead><tbody>
        ${Object.entries(a.modifier_links.by_source).map(([t, v]) => `<tr><td>${esc(t)}</td><td class="num">${v.links}</td><td class="num">${v.supported}</td><td class="num">${v.economy_unsupported}</td><td class="num">${v.out_of_scope}</td><td class="num">${v.missing}</td></tr>`).join("")}</tbody></table>
        <details><summary class="sub">지원하지 않는 Requirement 유형 ${a.requirements.unsupported_types.length}종 (데이터 내 ${a.requirements.types_in_data}종 중)</summary><div class="sub">${a.requirements.unsupported_types.map((t) => `${esc(t.type)} (${t.count})`).join(" · ")}</div></details></div>
      <div class="block"><h4><span>사용 중인 GlobalParameters</span></h4><div class="kv">${Object.entries(a.global_parameters_used).map(([k, v]) => `<span>${esc(k)}</span><span>${esc(v ?? "없음")}</span>`).join("")}</div></div>`;
  }

  // -------------------------------------------------------------- layout
  // Drag the gutters between panels to resize them. Sizes persist in localStorage.
  const LAYOUT_KEY = "civ6-layout-v1";
  const LAYOUT = {
    left: { min: 160, collapseBelow: 100, cls: "left-collapsed", css: "--left-size", panel: ".left-panel" },
    right: { min: 300, collapseBelow: 180, cls: "right-collapsed", css: "--right-size", panel: ".side-panel" },
    dock: { min: 120, collapseBelow: 80, cls: "dock-collapsed", css: "--dock-size", panel: ".bottom-dock" },
  };
  const MAP_MIN_W = 320, MAP_MIN_H = 140;
  let layout = {};
  function loadLayout() {
    try { layout = JSON.parse(localStorage.getItem(LAYOUT_KEY) || "{}") || {}; } catch (e) { layout = {}; }
  }
  function saveLayout() {
    try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout)); } catch (e) { /* ignore */ }
  }
  function shellBox() {
    const shell = document.querySelector(".app-shell");
    return { shell, w: shell.clientWidth - 16 - 16, h: shell.clientHeight - 16 - 8 };
  }
  function currentSize(key) {
    if (document.body.classList.contains(LAYOUT[key].cls)) return key === "dock" ? 38 : 0;
    const rect = document.querySelector(LAYOUT[key].panel).getBoundingClientRect();
    return key === "dock" ? rect.height : rect.width;
  }
  function maxSize(key) {
    const { w, h } = shellBox();
    if (key === "left") return Math.max(LAYOUT.left.min, w - currentSize("right") - MAP_MIN_W);
    if (key === "right") return Math.max(LAYOUT.right.min, w - currentSize("left") - MAP_MIN_W);
    return Math.max(LAYOUT.dock.min, h - MAP_MIN_H);
  }
  function applyLayout() {
    const { shell } = shellBox();
    for (const key of Object.keys(LAYOUT)) {
      const cfg = LAYOUT[key];
      document.body.classList.toggle(cfg.cls, Boolean(layout[key + "Collapsed"]));
      if (typeof layout[key] === "number") shell.style.setProperty(cfg.css, Math.round(Math.min(Math.max(layout[key], cfg.min), maxSize(key))) + "px");
      else shell.style.removeProperty(cfg.css);
    }
    const toggle = $("dockToggle");
    if (toggle) toggle.textContent = document.body.classList.contains("dock-collapsed") ? "▴" : "▾";
  }
  let rafPending = false;
  let canvasSize = null;
  function resizeKeepingCenter() {
    // Keep the map centred on the same spot while its panel grows or shrinks.
    const canvasEl = document.getElementById("mapCanvas");
    if (!canvasEl) return;
    const rect = canvasEl.getBoundingClientRect();
    if (canvasSize && state && typeof state.offsetX === "number") {
      state.offsetX += (rect.width - canvasSize.w) / 2;
      state.offsetY += (rect.height - canvasSize.h) / 2;
    }
    canvasSize = { w: rect.width, h: rect.height };
    resizeCanvas();
  }
  function relayout(final) {
    if (final) {
      resizeKeepingCenter();
      if (document.querySelector('[data-tabs="bottom"] .tab.active')?.dataset.tab === "chart") renderChart();
      return;
    }
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => { rafPending = false; resizeKeepingCenter(); });
  }
  function setSize(key, size) {
    const cfg = LAYOUT[key];
    if (size < cfg.collapseBelow) {
      layout[key + "Collapsed"] = true;
    } else {
      layout[key + "Collapsed"] = false;
      layout[key] = Math.round(Math.min(Math.max(size, cfg.min), maxSize(key)));
    }
    applyLayout();
  }
  function resetSize(key) {
    delete layout[key]; delete layout[key + "Collapsed"];
    applyLayout(); saveLayout(); relayout(true);
  }
  function applyPreset(name) {
    const { w, h } = shellBox();
    if (name === "default") layout = {};
    if (name === "map") layout = { leftCollapsed: true, right: 360, dockCollapsed: true };
    if (name === "analysis") layout = { left: 200, right: 380, dock: Math.round(h * 0.6) };
    if (name === "detail") layout = { left: 200, right: Math.round(w * 0.45), dock: 240 };
    applyLayout(); saveLayout(); relayout(true);
    if (typeof fitMap === "function") { fitMap(); canvasSize = null; resizeKeepingCenter(); }
    status("화면 배치를 바꿨습니다. 경계선을 끌어 더 조절할 수 있습니다.");
  }
  function setupLayout() {
    loadLayout();
    applyLayout();
    document.querySelectorAll(".splitter[data-split]").forEach((handle) => {
      const key = handle.dataset.split;
      const vertical = key !== "dock";
      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        handle.setPointerCapture(event.pointerId);
        const start = { x: event.clientX, y: event.clientY, size: currentSize(key) };
        handle.classList.add("dragging");
        document.body.classList.add("resizing", vertical ? "resizing-v" : "resizing-h");
        const move = (ev) => {
          const delta = key === "left" ? ev.clientX - start.x : key === "right" ? start.x - ev.clientX : start.y - ev.clientY;
          setSize(key, start.size + delta);
          relayout(false);
        };
        const up = () => {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          handle.removeEventListener("pointercancel", up);
          handle.classList.remove("dragging");
          document.body.classList.remove("resizing", "resizing-v", "resizing-h");
          saveLayout(); relayout(true);
          $("layoutPreset").value = "";
        };
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
        handle.addEventListener("pointercancel", up);
      });
      handle.addEventListener("dblclick", () => resetSize(key));
      handle.addEventListener("keydown", (event) => {
        const step = event.shiftKey ? 80 : 24;
        const grow = { left: "ArrowRight", right: "ArrowLeft", dock: "ArrowUp" }[key];
        const shrink = { left: "ArrowLeft", right: "ArrowRight", dock: "ArrowDown" }[key];
        if (event.key === grow || event.key === shrink) {
          event.preventDefault();
          const base = currentSize(key) || LAYOUT[key].collapseBelow;
          setSize(key, base + (event.key === grow ? step : -step));
          saveLayout(); relayout(true);
        } else if (event.key === "Enter") {
          layout[key + "Collapsed"] = !layout[key + "Collapsed"];
          applyLayout(); saveLayout(); relayout(true);
        }
      });
    });
    $("layoutPreset").onchange = (e) => { if (e.target.value) applyPreset(e.target.value); };
    $("dockToggle").onclick = () => {
      layout.dockCollapsed = !document.body.classList.contains("dock-collapsed");
      applyLayout(); saveLayout(); setTimeout(() => relayout(true), 30);
    };
    window.addEventListener("resize", () => { applyLayout(); canvasSize = null; });
  }

  // ---------------------------------------------------------------- init
  async function init() {
    legacySetStatus = setStatus;
    // Route legacy status messages through a debounced simulation refresh.
    window.setStatus = setStatus = function (msg) { legacySetStatus(msg); scheduleRefresh(); };
    const originalSelect = selectCity;
    window.selectCity = selectCity = function (id) {
      S.suppress++;
      try { originalSelect(id); } finally { S.suppress--; }
      S.prod = null; S.optimize = null;
      renderCityCards();
      void refreshCity().then(() => {
        if (document.querySelector('[data-tabs="right"] .tab.active')?.dataset.tab === "production") void loadProductionOptions();
        renderHistory(); renderChart();
      });
    };
    const originalRender = renderCities;
    window.renderCities = renderCities = function () { originalRender(); renderCityCards(); };
    setupTabs();
    setupLayout();
    try { S.presets = (await api("/api/sim/strategies", "GET", undefined, false)).presets; } catch (e) { S.presets = []; }
    $("simRunTurnBtn").onclick = () => runTurns();
    $("simPreviewBtn").onclick = previewTurns;
    $("scenarioSelect").onchange = (e) => switchScenario(e.target.value);
    $("overlayMode").onchange = (e) => { S.overlay = e.target.value; computeOverlayMax(); drawMap(); };
    $("citizenEditBtn").onclick = () => { S.citizenEdit = !S.citizenEdit; $("citizenEditBtn").classList.toggle("on", S.citizenEdit); drawMap(); };
    window.addEventListener("resize", () => setTimeout(renderTop, 50));
    await loadScenarios();
    await loadHistory();
    await refreshAll();
    computeOverlayMax();
    resizeCanvas();
    fitMap();
    renderLog();
  }

  // Apply the saved panel sizes immediately so the page does not jump after loading.
  try { loadLayout(); applyLayout(); } catch (error) { /* layout is optional */ }

  // ------------------------------------------------------------ difficulty
  async function openDifficulty() {
    const dlg = document.getElementById("difficultyDialog"); if (!dlg) return;
    let data;
    try { data = await api("/api/sim/difficulties"); } catch (error) { status("난이도 정보를 불러오지 못했습니다: " + error.message); return; }
    const opts = (sel) => data.levels.map((l) => `<option value="${esc(l.id)}" ${l.id === sel ? "selected" : ""}>${esc(l.label)}</option>`).join("");
    $("difficultyRows").innerHTML = data.players.map((p) => `<tr data-key="${esc(p.key)}"${p.key === data.current ? ' class="current"' : ""}>
      <td>${esc(p.label)}${p.key === data.current ? ' <span class="badge s-verified">지금 보는 문명</span>' : ""}</td>
      <td class="num">${p.cities == null ? "" : esc(p.cities)}</td>
      <td><select data-diff>${opts(p.difficulty)}</select></td>
      <td><select data-human><option value="ai" ${p.human ? "" : "selected"}>AI</option><option value="human" ${p.human ? "selected" : ""}>사람</option></select></td></tr>`).join("");
    $("difficultyAll").innerHTML = opts(data.default);
    $("difficultyAllApply").onclick = () => {
      document.querySelectorAll("#difficultyRows tr").forEach((tr) => {
        if (tr.querySelector("[data-human]").value === "ai") tr.querySelector("[data-diff]").value = $("difficultyAll").value;
      });
    };
    const cell = (r, i) => {
      const v = r.values[i], d = (r.data_values || r.values)[i];
      const changed = Math.abs(v - d) > 1e-9;
      return `<td class="num${changed ? " overridden" : ""}" title="게임 데이터: ${esc(num(d, 1))}"><input type="number" step="any" class="bonus-input" data-mod="${esc(r.modifier)}" data-level="${esc(data.levels[i].id)}" data-orig="${esc(d)}" value="${esc(v)}"></td>`;
    };
    $("difficultyTable").innerHTML = `<table class="grid bonus-table"><thead><tr><th>대상</th><th>효과</th>${data.levels.map((l) => `<th class="num">${esc(l.label)}</th>`).join("")}</tr></thead><tbody>${
      data.table.map((r) => `<tr${r.applies_to_yields ? ' class="yields"' : ""}><td>${esc(r.who)}</td><td>${esc(r.label)}${r.applies_to_yields ? " <b>· 산출 반영</b>" : ""}</td>${
        r.values.map((_, i) => cell(r, i)).join("")}</tr>`).join("")}</tbody></table>
      <div class="form-row"><button type="button" class="mini" id="difficultyResetBonus">보너스 표를 게임 데이터 값으로 되돌리기</button></div>
      <p class="sub">AI 추가 % (게임 데이터에 없음, 관전 세이브 비교로 맞춘 값): ${esc(Object.entries(data.ai_extra_percent || {}).map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${v}%`).join(", ") || "없음")} — ${esc(data.config_file || "app/config/difficulty.json")}의 ai_extra_percent에서 바꿉니다. % 보정은 게임처럼 1/256 단위로 반올림해 더합니다(24% → 23.83%).</p>
      <p class="sub">칸을 고치고 적용하면 ${esc(data.config_file || "app/config/difficulty.json")}에 저장되어 다음 실행에도 유지됩니다. 노란 칸은 게임 데이터와 다른 값입니다(마우스를 올리면 원래 값). 산출 반영 표시가 있는 줄만 도시 계산에 들어갑니다. 전투력·경험치·무료 유레카/영감·시작 유닛은 도시 산출과 무관해 참고로만 보여 줍니다.</p>`;
    const mark = (input) => input.closest("td").classList.toggle("overridden", Math.abs(Number(input.value) - Number(input.dataset.orig)) > 1e-9);
    document.querySelectorAll("#difficultyTable .bonus-input").forEach((input) => { input.oninput = () => mark(input); });
    $("difficultyResetBonus").onclick = () => document.querySelectorAll("#difficultyTable .bonus-input").forEach((input) => { input.value = input.dataset.orig; mark(input); });
    dlg.returnValue = "";
    dlg.showModal();
    const answer = await new Promise((resolve) => dlg.addEventListener("close", () => resolve(dlg.returnValue), { once: true }));
    if (answer !== "save") return;
    const players = {};
    document.querySelectorAll("#difficultyRows tr").forEach((tr) => {
      players[tr.dataset.key] = { difficulty: tr.querySelector("[data-diff]").value, human: tr.querySelector("[data-human]").value === "human" };
    });
    await guarded(async () => {
      const bonus_overrides = {};
      document.querySelectorAll("#difficultyTable .bonus-input").forEach((input) => {
        const value = Number(input.value);
        if (input.value !== "" && Number.isFinite(value) && Math.abs(value - Number(input.dataset.orig)) > 1e-9) {
          (bonus_overrides[input.dataset.mod] ||= {})[input.dataset.level] = value;
        }
      });
      const result = await api("/api/sim/difficulties", "PUT", { players, bonus_overrides });
      S.difficulties = { default: result.default, players: Object.fromEntries(result.players.map((p) => [p.key, { difficulty: p.difficulty, human: p.human }])) };
      if (typeof state !== "undefined" && state.game) state.game.difficulties = S.difficulties;
      const changed = (result.impact || []).filter((i) => Object.values(i.delta).some((v) => Math.abs(v) > 1e-9));
      status(changed.length ? `난이도 적용: ${changed.length}개 도시 산출이 바뀌었습니다.` : "난이도 적용: 지금 보는 문명의 산출은 그대로입니다.");
      S.yieldLedgerStale = true;
      await refreshAll();
    });
  }
  const diffBtn = document.getElementById("difficultyBtn");
  if (diffBtn) diffBtn.onclick = () => void openDifficulty();

  function invalidate() {
    S.prod = null; S.empire = null; S.optimize = null; S.difficulties = null;
    S.yieldLedger = null; S.yieldLedgerStale = true; S.ledger = null;
    if (document.querySelector('[data-tabs="right"] .tab.active')?.dataset.tab === "empire") void loadEmpire();
  }

  window.SimUI = {
    invalidate, init, runTurns, overlayActive, decorateTile, markTile, afterDraw, onTileClick, detailsAt,
    refreshAll, refreshCity, _state: S,
  };
})();
