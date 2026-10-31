// Vote list on a profile page: person codes + shared per-term event lists.
(function () {
  const script = document.currentScript;
  const LABEL = { y: "pro", n: "proti", a: "zdržel/a se", v: "nehlasoval/a", x: "nepřítomen/a", e: "omluven/a" };
  const OUTCOME = { passed: "přijato", rejected: "zamítnuto", void: "zmatečné", other: "" };
  const PAGE = 50;
  const $ = (id) => document.getElementById(id);
  const cache = {};
  let person, rows = [], page = 0;

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const date = (d) => { const [y, m, dd] = d.split("-"); return `${+dd}. ${+m}. ${y}`; };

  async function events(key) {
    if (!cache[key]) {
      const r = await fetch(`../v/${key}.json`);
      const data = await r.json();
      data.events.forEach((ev) => (ev.push(fold(ev[1] + " " + ev[2] + " " + ev[5]))));
      cache[key] = data.events;
    }
    return cache[key];
  }

  async function refresh() {
    const t = person.terms.find((x) => x.key === $("term").value);
    if (!t) return;
    const evs = await events(t.key);
    const q = fold($("q").value.trim());
    const words = q ? q.split(/\s+/) : [];
    const opt = $("opt").value, dev = $("dev").checked;
    rows = [];
    for (let i = evs.length - 1; i >= 0; i--) {
      const c = t.votes[i];
      if (c === "." || (opt && c !== opt)) continue;
      const l = t.line[i];
      if (dev && (l === "." || !"yna".includes(c) || c === l)) continue;
      if (words.length && !words.every((w) => evs[i][12].includes(w))) continue;
      rows.push([evs[i], c, l]);
    }
    page = 0;
    render();
  }

  function render() {
    const body = $("votes").querySelector("tbody");
    const slice = rows.slice(page * PAGE, (page + 1) * PAGE);
    body.innerHTML = slice.map(([ev, c, l]) => {
      const [d, title, detail, outcome, url, bill, billUrl, yes, no, abst, session, number] = ev;
      const differs = l !== "." && "yna".includes(c) && c !== l;
      const counts = yes != null ? ` <span class="muted" title="pro – proti – zdrželo se">${yes}–${no}–${abst}</span>` : "";
      const billLink = bill ? (billUrl ? ` · <a href="${esc(billUrl)}">tisk ${esc(bill)}</a>` : ` · ${esc(bill)}`) : "";
      return `<tr${differs ? ' class="differs"' : ""}><td class="nowrap">${date(d)}</td>` +
        `<td><a href="${esc(url)}">${esc(title || "(bez názvu)")}</a>${detail && detail !== title ? `<div class="muted">${esc(detail)}</div>` : ""}<div class="muted small">${session ? `schůze ${esc(session)}, ` : ""}${number ? `hlasování č. ${esc(number)}` : ""}${billLink}</div></td>` +
        `<td class="opt">${LABEL[c] || c}</td><td class="club">${l === "." ? "" : LABEL[l]}${differs ? ' <span class="flag">jinak</span>' : ""}</td>` +
        `<td class="res">${OUTCOME[outcome] ?? esc(outcome)}${counts}</td></tr>`;
    }).join("");
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    $("count").textContent = `${rows.length.toLocaleString("cs-CZ")} hlasování`;
    $("pageinfo").textContent = `${page + 1} / ${pages}`;
    $("prev").disabled = page === 0;
    $("next").disabled = page >= pages - 1;
  }

  async function init() {
    person = await (await fetch(script.dataset.person.replace(/^.*\//, ""))).json();
    if (!person.terms.length) { document.getElementById("seznam").hidden = true; return; }
    $("term").innerHTML = person.terms.map((t) => `<option value="${t.key}">${esc(t.label)}</option>`).join("");
    $("term").value = person.terms[person.terms.length - 1].key;
    let timer;
    $("q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(refresh, 150); });
    ["term", "opt", "dev"].forEach((id) => $(id).addEventListener("change", refresh));
    $("prev").addEventListener("click", () => { page--; render(); });
    $("next").addEventListener("click", () => { page++; render(); });
    refresh();
  }
  init();
})();
