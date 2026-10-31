// Politician list with instant search on the index page.
(function () {
  const data = JSON.parse(document.getElementById("data").textContent);
  const fold = (s) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  data.forEach((p) => (p.key = fold(`${p.n} ${p.k} ${p.c.join(" ")}`)));
  const q = document.getElementById("q"), active = document.getElementById("active");
  const body = document.querySelector("#people tbody"), count = document.getElementById("count");
  function render() {
    const words = fold(q.value.trim()).split(/\s+/).filter(Boolean);
    const rows = data.filter((p) => (!active.checked || p.a) && words.every((w) => p.key.includes(w)));
    count.textContent = `${rows.length} politiků`;
    body.innerHTML = rows.map((p) => `<tr><td><a href="p/${p.s}.html">${esc(p.n)}</a></td><td>${esc(p.k)}</td><td class="muted">${esc(p.c.join(", "))}</td></tr>`).join("");
  }
  q.addEventListener("input", render);
  active.addEventListener("change", render);
  render();
})();
