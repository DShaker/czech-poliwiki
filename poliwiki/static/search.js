// Politician list on the index page: search, chamber and club filters, sorting.
// State lives in the URL hash (#komora=psp&klub=ODS&razeni=club) so a filtered view can be shared.
(function () {
  const data = JSON.parse(document.getElementById("data").textContent);
  const CH = { psp: "Sněmovna", senat: "Senát", ep: "EP" };
  const CH_ORDER = { psp: 0, senat: 1, ep: 2 };
  const NONE = "-";   // chip value for people without a club
  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const coll = new Intl.Collator("cs");
  const $ = (id) => document.getElementById(id);

  data.forEach((p) => {
    p.club = p.k || p.kl || "";
    p.key = fold(`${p.n} ${p.club} ${p.c.join(" ")}`);
  });

  const q = $("q"), active = $("active"), sortSel = $("sort");
  const seg = $("chamber"), chips = $("clubs");
  const body = document.querySelector("#people tbody"), count = $("count"), empty = $("empty");
  const st = { ch: "", club: "", sort: "name", desc: false };

  function readHash() {
    const h = new URLSearchParams(location.hash.slice(1));
    st.ch = CH[h.get("komora")] ? h.get("komora") : "";
    st.club = h.get("klub") || "";
    const [s, d] = (h.get("razeni") || "name").split("-");
    st.sort = ["name", "club", "chamber"].includes(s) ? s : "name";
    st.desc = d === "desc";
    active.checked = h.get("vse") !== "1";
    q.value = h.get("q") || "";
  }
  function writeHash() {
    const h = new URLSearchParams();
    if (st.ch) h.set("komora", st.ch);
    if (st.club) h.set("klub", st.club);
    if (st.sort !== "name" || st.desc) h.set("razeni", st.sort + (st.desc ? "-desc" : ""));
    if (!active.checked) h.set("vse", "1");
    if (q.value.trim()) h.set("q", q.value.trim());
    const s = h.toString();
    history.replaceState(null, "", s ? `#${s}` : location.pathname + location.search);
  }

  const chambersOf = (p) => (active.checked ? p.hc : p.h);
  const cmpName = (a, b) => coll.compare(a.f, b.f) || coll.compare(a.n, b.n);
  const SORTS = {
    name: cmpName,
    club: (a, b) => (!a.club - !b.club) || coll.compare(a.club, b.club) || cmpName(a, b),
    chamber: (a, b) => (CH_ORDER[chambersOf(a)[0]] ?? 9) - (CH_ORDER[chambersOf(b)[0]] ?? 9) || cmpName(a, b),
  };

  function render() {
    const words = fold(q.value.trim()).split(/\s+/).filter(Boolean);
    const base = data.filter((p) => !active.checked || p.a);
    const byWords = base.filter((p) => words.every((w) => p.key.includes(w)));

    // chamber buttons with counts (after search, before chamber/club)
    seg.querySelectorAll("button").forEach((b) => {
      const c = b.dataset.ch;
      b.setAttribute("aria-pressed", String(c === st.ch));
      b.querySelector(".n").textContent = c ? byWords.filter((p) => chambersOf(p).includes(c)).length : byWords.length;
    });

    // club chips: clubs present in the chosen chamber, alphabetical
    const inCh = byWords.filter((p) => !st.ch || chambersOf(p).includes(st.ch));
    const allInCh = base.filter((p) => !st.ch || chambersOf(p).includes(st.ch));
    const tally = new Map();
    allInCh.forEach((p) => tally.set(p.club || NONE, 0));
    inCh.forEach((p) => tally.set(p.club || NONE, tally.get(p.club || NONE) + 1));
    if (st.club && !tally.has(st.club)) st.club = "";
    // with all chambers shown, group the chips by the chamber the club belongs to
    const home = new Map();
    allInCh.forEach((p) => p.club && home.set(p.club, p.kh));
    const group = (c) => (st.ch || c === NONE ? 9 : CH_ORDER[home.get(c)] ?? 9);
    const clubs = [...tally.keys()].sort((a, b) => group(a) - group(b) || (a === NONE) - (b === NONE) || coll.compare(a, b));
    const chip = (c) => `<button type="button" data-club="${esc(c)}" aria-pressed="${c === st.club}">`
      + `${c === NONE ? "bez klubu" : esc(c)} <span class="n">${tally.get(c)}</span></button>`;
    let html = `<button type="button" data-club="" aria-pressed="${!st.club}">všechny</button>`, prev = null;
    clubs.forEach((c) => {
      const g = group(c);
      if (!st.ch && g !== prev && g !== 9) html += `<span class="chipgroup">${CH[home.get(c)]}</span>`;
      prev = g;
      html += chip(c);
    });
    chips.innerHTML = html;

    const rows = inCh.filter((p) => !st.club || (p.club || NONE) === st.club);
    rows.sort(SORTS[st.sort]);
    if (st.desc) rows.reverse();

    sortSel.value = st.sort;
    document.querySelectorAll("#people th").forEach((th) => {
      const b = th.querySelector("button");
      if (b && b.dataset.sort === st.sort) th.setAttribute("aria-sort", st.desc ? "descending" : "ascending");
      else th.removeAttribute("aria-sort");
    });

    const n = rows.length;
    count.textContent = `${n} ${n === 1 ? "politik" : n >= 2 && n <= 4 ? "politici" : "politiků"}`;
    empty.hidden = n > 0;
    body.innerHTML = rows.map((p) => {
      const chs = chambersOf(p).map((c) => `<span class="badge">${CH[c]}</span>`).join(" ");
      const club = p.k ? esc(p.k) : p.kl ? `<span class="muted" title="poslední klub">${esc(p.kl)}</span>` : "";
      return `<tr><td class="name"><a href="p/${p.s}.html">${esc(p.n)}</a></td><td class="ch">${chs}</td>`
        + `<td class="club">${club}</td><td class="mand muted">${esc(p.c.join(", "))}</td></tr>`;
    }).join("");
    writeHash();
  }

  function setSort(key, toggle) {
    st.desc = toggle && st.sort === key ? !st.desc : false;
    st.sort = key;
    render();
  }

  q.addEventListener("input", render);
  active.addEventListener("change", render);
  sortSel.addEventListener("change", () => setSort(sortSel.value, false));
  document.querySelectorAll("#people th button").forEach((b) => b.addEventListener("click", () => setSort(b.dataset.sort, true)));
  seg.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    st.ch = b.dataset.ch;
    render();
  });
  chips.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    st.club = b.dataset.club === st.club ? "" : b.dataset.club;
    render();
  });
  $("reset").addEventListener("click", () => {
    st.ch = ""; st.club = ""; q.value = ""; active.checked = true;
    render();
  });
  window.addEventListener("hashchange", () => { readHash(); render(); });
  readHash();
  render();
})();
