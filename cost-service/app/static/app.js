// Тема: авто (как в системе) → тёмная → светлая; выбор запоминается
(function () {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const root = document.documentElement;
  const names = {auto: "как в системе", dark: "тёмная", light: "светлая"};
  const current = () => root.dataset.theme || "auto";
  const show = () => {
    const mode = current();
    btn.querySelectorAll("[data-theme-icon]").forEach(el => (el.hidden = el.dataset.themeIcon !== mode));
    btn.title = `Тема: ${names[mode]} (нажмите, чтобы сменить)`;
  };
  btn.addEventListener("click", () => {
    const next = {auto: "dark", dark: "light", light: "auto"}[current()];
    if (next === "auto") delete root.dataset.theme; else root.dataset.theme = next;
    try { next === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", next); } catch (e) {}
    show();
  });
  show();
})();

// Боковое меню: свернуть на компьютере, выезжающая панель на телефоне
(function () {
  const root = document.documentElement;
  const collapse = document.getElementById("collapse-btn");
  const toggle = document.getElementById("menu-toggle");
  const backdrop = document.getElementById("backdrop");
  if (collapse) collapse.addEventListener("click", () => {
    const collapsed = root.classList.toggle("sidebar-collapsed");
    collapse.title = collapsed ? "Развернуть меню" : "Свернуть меню";
    try { collapsed ? localStorage.setItem("sidebar", "collapsed") : localStorage.removeItem("sidebar"); } catch (e) {}
  });
  if (toggle) toggle.addEventListener("click", () => root.classList.toggle("sidebar-open"));
  if (backdrop) backdrop.addEventListener("click", () => root.classList.remove("sidebar-open"));
})();


// Выделение ресурсов сервера сервису: стоимость считается сразу при вводе.
// .alloc[data-full, data-overhead, data-capcost] > .alloc-whole, .alloc-amount[data-price], .alloc-cost
(function () {
  const currency = document.body.dataset.currency || "";
  const num = (v) => { const x = parseFloat(String(v || "").replace(/\s/g, "").replace(",", ".")); return isFinite(x) ? x : 0; };
  const money = (v) => v.toLocaleString("ru-RU", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + " " + currency;
  document.querySelectorAll(".alloc").forEach(box => {
    const whole = box.querySelector(".alloc-whole");
    const inputs = [...box.querySelectorAll(".alloc-amount")];
    const out = box.querySelector(".alloc-cost");
    const recalc = () => {
      const isWhole = whole && whole.checked;
      inputs.forEach(i => { i.disabled = isWhole; });
      box.querySelectorAll(".alloc-amounts").forEach(el => el.classList.toggle("dimmed", isWhole));
      let cost;
      if (isWhole) cost = num(box.dataset.full);
      else {
        const res = inputs.reduce((sum, i) => sum + num(i.value) * num(i.dataset.price), 0);
        const capcost = num(box.dataset.capcost);
        cost = res + (capcost ? num(box.dataset.overhead) * res / capcost : 0);
      }
      if (out) out.textContent = money(cost);
    };
    box.addEventListener("input", recalc);
    box.addEventListener("change", recalc);
    box.querySelectorAll(".alloc-fill").forEach(btn => btn.addEventListener("click", () => {
      const input = box.querySelector(`.alloc-amount[data-key="${btn.dataset.key}"]`);
      input.value = btn.dataset.value;
      recalc();
    }));
    recalc();
  });
})();
