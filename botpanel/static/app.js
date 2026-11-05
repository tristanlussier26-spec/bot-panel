"use strict";
/* Bot Panel : interface (sans framework). Les pages sont dans l'adresse après le # : #/, #/bot/ID, #/serveurs, #/admin */

// ---------------------------------------------------------------------------------------------
// Outils
// ---------------------------------------------------------------------------------------------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** Remplace le contenu d'un élément (accepte des listes et ignore null/false, comme h()). */
function fill(el, ...children) {
  el.textContent = "";
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

async function api(method, url, body, { raw = false } = {}) {
  const opts = { method, headers: { "X-Panel": "1" } };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 401) { location.href = "/login"; throw new ApiError("Session expirée", 401); }
  if (raw) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Erreur ${res.status}`, res.status);
  return data;
}

let toastTimer;
function toast(message, bad = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 6000 : 3000);
}

/** Exécute une action d'un bouton : le désactive pendant l'action et affiche l'erreur s'il y en a une. */
async function act(button, fn, done) {
  if (button) button.disabled = true;
  try {
    const result = await fn();
    if (done) toast(done);
    return result;
  } catch (e) {
    toast(e.message, true);
    return undefined;
  } finally {
    if (button) button.disabled = false;
  }
}

const fmtMB = (mb) => (mb > 0 && mb < 1 ? "moins de 1 Mo" : mb >= 1024 ? `${(mb / 1024).toFixed(1).replace(".", ",")} Go` : `${Math.round(mb)} Mo`);
const fmtBytes = (b) => (b < 1024 ? `${b} o` : b < 1048576 ? `${Math.round(b / 1024)} Ko` : `${(b / 1048576).toFixed(1).replace(".", ",")} Mo`);
const fmtDate = (ts) => new Date(ts * 1000).toLocaleString("fr-CA", { dateStyle: "medium", timeStyle: "short" });
const LANGS = { python: "Python", node: "Node.js", custom: "Image perso" };
const STATES = {
  running: ["on", "En marche"], starting: ["wait", "Démarrage…"], offline: ["off", "Arrêté"],
  restarting: ["wait", "Redémarrage…"], paused: ["wait", "En pause"], unknown: ["off", "Inconnu"],
};
function stateOf(bot) {
  const s = bot.status || {};
  if (s.state === "offline" && bot.wanted_running) return ["wait", "Démarrage…"];
  if (s.state === "offline" && s.exit_code && s.exit_code !== 0 && s.exit_code !== 137 && s.exit_code !== 143)
    return ["bad", s.oom ? "Arrêté : manque de RAM" : `Planté (code ${s.exit_code})`];
  return STATES[s.state] || ["off", s.state || "?"];
}
const lamp = (kind) => h("i", { class: `lamp ${kind}`, "aria-hidden": "true" });

let ME = null;
let cleanup = [];
const view = () => document.getElementById("view");
function mount(...nodes) {
  const v = view();
  fill(v, ...nodes);
}
function onLeave(fn) { cleanup.push(fn); }

// ---------------------------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------------------------
async function route() {
  cleanup.forEach((fn) => { try { fn(); } catch { /* rien */ } });
  cleanup = [];
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const section = parts[0] || "";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    a.classList.toggle("current", a.dataset.nav === (section === "bot" || section === "nouveau" || !section ? "bots" : section));
  });
  mount(h("p", { class: "loading" }, "Chargement…"));
  try {
    if (!section) await pageBots();
    else if (section === "nouveau") await pageNewBot();
    else if (section === "bot" && parts[1]) await pageBot(parts[1], parts[2] || "console");
    else if (section === "serveurs") await pageBuilder();
    else if (section === "admin" && ME.role === "admin") await pageAdmin();
    else mount(h("div", { class: "empty" }, h("h2", {}, "Page introuvable"), h("a", { class: "btn", href: "#/" }, "Retour aux bots")));
  } catch (e) {
    mount(h("div", { class: "banner" }, e.message), h("a", { class: "btn", href: "#/" }, "Retour aux bots"));
  }
  view().focus({ preventScroll: true });
}

// ---------------------------------------------------------------------------------------------
// Liste des bots
// ---------------------------------------------------------------------------------------------
function powerButton(bot, refresh) {
  const running = bot.status && bot.status.state === "running";
  const btn = h("button", { class: `btn small ${running ? "" : "go"}`, type: "button" }, running ? "Arrêter" : "Démarrer");
  btn.addEventListener("click", (ev) => {
    ev.preventDefault();
    act(btn, () => api("POST", `/api/bots/${bot.id}/power`, { action: running ? "stop" : "start" }), running ? "Bot arrêté" : "Bot démarré").then(refresh);
  });
  return btn;
}

async function pageBots() {
  const all = ME.role === "admin" && sessionStorage.getItem("tous") === "1";
  const bots = await api("GET", `/api/bots${all ? "?tous=1" : ""}`);
  const head = h("div", { class: "pagehead" },
    h("div", {}, h("h1", {}, all ? "Tous les bots" : "Tes bots"),
      h("p", { class: "soft" }, "Chaque bot roule dans son propre conteneur, avec sa console, ses fichiers et ses limites.")),
    h("div", { class: "formactions" },
      ME.role === "admin" ? h("label", { class: "check" },
        h("input", { type: "checkbox", checked: all, onchange: (e) => { sessionStorage.setItem("tous", e.target.checked ? "1" : "0"); route(); } }),
        "Voir les bots de tout le monde") : null,
      h("a", { class: "btn primary", href: "#/nouveau" }, "Nouveau bot")));
  const banner = ME.docker ? null : h("div", { class: "banner" }, `Docker est pas joignable : ${ME.docker_error}`);
  if (!bots.length) {
    mount(head, banner, h("div", { class: "rack" }, h("div", { class: "empty" },
      h("h2", {}, "Aucun bot pour l'instant"),
      h("p", { class: "soft" }, "Crée ton premier bot en Python ou en Node.js. On met un petit bot d'exemple dedans : ajoute son jeton et démarre-le."),
      h("a", { class: "btn primary", href: "#/nouveau" }, "Créer un bot"))));
    return;
  }
  const rack = h("div", { class: "rack" }, bots.map((bot) => {
    const [kind, label] = stateOf(bot);
    const st = bot.stats;
    const pct = st ? Math.min(100, (st.memory_mb / st.memory_limit_mb) * 100) : 0;
    return h("a", { class: "slot", href: `#/bot/${bot.id}` },
      lamp(kind),
      h("div", {}, h("div", { class: "slot-name" }, bot.name),
        h("div", { class: "slot-sub" }, label, bot.owner_name ? ` (à ${bot.owner_name})` : "")),
      h("div", { class: "slot-mid slot-sub" }, LANGS[bot.language], h("br"), bot.image),
      h("div", { class: "slot-meter slot-sub" },
        st ? `RAM ${fmtMB(st.memory_mb)} sur ${fmtMB(bot.ram_mb)}, CPU ${st.cpu_percent} %` : `RAM max ${fmtMB(bot.ram_mb)}`,
        h("div", { class: `meter ${pct > 90 ? "hot" : ""}` }, h("i", { style: null, dataset: { w: pct } }))),
      h("div", { class: "slot-power" }, powerButton(bot, route)));
  }));
  rack.querySelectorAll(".meter > i").forEach((i) => { i.style.width = `${i.dataset.w}%`; });
  const u = ME.usage;
  const quota = ME.role === "admin" ? null : h("div", { class: "quota" },
    h("span", {}, "Bots ", h("strong", {}, `${u.bots} sur ${ME.max_bots}`)),
    h("span", {}, "RAM réservée ", h("strong", {}, `${fmtMB(u.ram_mb)} sur ${fmtMB(ME.max_ram_mb)}`)),
    h("span", {}, "Disque ", h("strong", {}, `${fmtMB(u.disk_mb)} sur ${fmtMB(ME.max_disk_mb)}`)));
  mount(head, banner, rack, quota);
  const timer = setInterval(async () => {
    if (document.hidden) return;
    const fresh = await api("GET", `/api/bots${all ? "?tous=1" : ""}`).catch(() => null);
    if (fresh && JSON.stringify(fresh.map((b) => [b.id, b.status.state, b.wanted_running])) !== JSON.stringify(bots.map((b) => [b.id, b.status.state, b.wanted_running]))) route();
  }, 5000);
  onLeave(() => clearInterval(timer));
}

// ---------------------------------------------------------------------------------------------
// Nouveau bot / réglages
// ---------------------------------------------------------------------------------------------
function settingsForm(bot, onSubmit, submitLabel) {
  const isNew = !bot;
  bot = bot || { language: "python", ram_mb: 256, cpu_percent: 100, auto_restart: true, main_file: "", image: "" };
  const form = h("form", { class: "panelbox" });
  const imageSelect = h("select", { name: "image", id: "f-image" });
  const imageCustom = h("input", { type: "text", name: "image_custom", id: "f-image-custom", placeholder: "ex. eclipse-temurin:21-jre", value: bot.language === "custom" ? bot.image : "" });
  const mainInput = h("input", { type: "text", name: "main_file", id: "f-main", value: bot.main_file, placeholder: "main.py" });
  const cmdField = h("div", { class: "field" },
    h("label", { for: "f-cmd" }, "Commande de démarrage",
      h("span", { class: "hint" }, "Vide = la commande normale du langage (installe les dépendances puis lance le fichier principal).")),
    h("input", { type: "text", name: "startup_cmd", id: "f-cmd", value: bot.startup_cmd || "", placeholder: "ex. java -jar bot.jar" }));

  function fillImages(lang) {
    const choices = ME.images.filter((i) => (lang === "python" ? i.startsWith("python") : i.startsWith("node")));
    const def = ME.defaults[lang];
    if (def && !choices.includes(def)) choices.unshift(def);
    if (bot.image && bot.language === lang && !choices.includes(bot.image)) choices.unshift(bot.image);
    fill(imageSelect, ...choices.map((i) => h("option", { value: i }, i)));
    imageSelect.value = bot.language === lang && bot.image ? bot.image : def;
    mainInput.placeholder = lang === "node" ? "index.js" : "main.py";
    imageSelect.closest(".field").hidden = lang === "custom";
    imageCustom.closest(".field").hidden = lang !== "custom";
  }
  const langs = h("div", { class: "segmented", role: "radiogroup", "aria-label": "Langage" },
    Object.entries(LANGS).map(([value, label]) => h("label", {},
      h("input", { type: "radio", name: "language", value, checked: bot.language === value, onchange: () => fillImages(value) }), label)));

  form.append(
    h("div", { class: "field" }, h("label", { for: "f-name" }, "Nom du bot"),
      h("input", { type: "text", id: "f-name", name: "name", value: bot.name || "", maxlength: 40, required: true, placeholder: "ex. Bot musique" })),
    h("div", { class: "field" }, h("label", {}, "Langage"), langs,
      isNew ? h("span", { class: "hint" }, "Python et Node.js : on crée un petit bot d'exemple que tu peux remplacer par tes fichiers.") : null),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("label", { for: "f-image" }, "Version"), imageSelect),
      h("div", { class: "field", hidden: true }, h("label", { for: "f-image-custom" }, "Image Docker",
        h("span", { class: "hint" }, ME.role === "admin" ? "N'importe quelle image publique." : `Images permises : ${ME.images.join(", ")}`)), imageCustom),
      h("div", { class: "field" }, h("label", { for: "f-main" }, "Fichier principal"), mainInput)),
    cmdField,
    h("div", { class: "row" },
      h("div", { class: "field" }, h("label", { for: "f-ram" }, "RAM max (Mo)"),
        h("input", { type: "number", id: "f-ram", name: "ram_mb", min: 64, max: 16384, step: 64, value: bot.ram_mb })),
      h("div", { class: "field" }, h("label", { for: "f-cpu" }, "CPU max (%)"),
        h("input", { type: "number", id: "f-cpu", name: "cpu_percent", min: 10, max: 800, step: 10, value: bot.cpu_percent }),
        h("span", { class: "hint" }, "100 % = un cœur au complet."))),
    h("div", { class: "field" }, h("label", { class: "check" },
      h("input", { type: "checkbox", name: "auto_restart", checked: bot.auto_restart }),
      h("span", {}, "Redémarrer tout seul s'il plante", h("span", { class: "hint" }, "S'il plante deux fois en moins d'une minute, le panel arrête d'essayer.")))),
    h("div", { class: "field" }, h("label", { class: "check" },
      h("input", { type: "checkbox", name: "web_port", checked: !!bot.port }),
      h("span", {}, "Ouvrir un port web",
        h("span", { class: "hint" }, bot.port
          ? `Port ${bot.port} : ton bot doit écouter sur la variable SERVER_PORT. Adresse : ${bot.web_url}`
          : "Pour un bot qui a son propre site ou son panel web. Le panel lui donne un port et la variable SERVER_PORT.")))),
  );
  if (isNew) {
    form.append(h("details", { class: "field" }, h("summary", {}, "Déployer depuis GitHub (facultatif)"),
      h("div", { class: "field" }, h("label", { for: "f-git" }, "Adresse du dépôt"),
        h("input", { type: "url", id: "f-git", name: "git_url", placeholder: "https://github.com/toi/mon-bot" })),
      h("div", { class: "row" },
        h("div", { class: "field" }, h("label", { for: "f-branch" }, "Branche"),
          h("input", { type: "text", id: "f-branch", name: "git_branch" }), h("span", { class: "hint" }, "Vide = la branche principale.")),
        h("div", { class: "field" }, h("label", { for: "f-gtoken" }, "Jeton GitHub"),
          h("input", { type: "password", id: "f-gtoken", name: "git_token", autocomplete: "off" }),
          h("span", { class: "hint" }, "Seulement pour un dépôt privé (lecture seule).")))));
  }
  const submit = h("button", { class: "btn primary", type: "submit" }, submitLabel);
  form.append(h("div", { class: "formactions" }, submit, isNew ? h("a", { class: "btn", href: "#/" }, "Annuler") : null));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fd = new FormData(form);
    const lang = fd.get("language");
    const data = {
      name: fd.get("name"), language: lang,
      image: lang === "custom" ? fd.get("image_custom") : fd.get("image"),
      main_file: fd.get("main_file") || (lang === "node" ? "index.js" : "main.py"),
      startup_cmd: fd.get("startup_cmd"), ram_mb: Number(fd.get("ram_mb")), cpu_percent: Number(fd.get("cpu_percent")),
      auto_restart: fd.get("auto_restart") === "on", web_port: fd.get("web_port") === "on",
    };
    if (isNew && fd.get("git_url")) Object.assign(data, { git_url: fd.get("git_url"), git_branch: fd.get("git_branch"), git_token: fd.get("git_token") || null });
    act(submit, () => onSubmit(data));
  });
  queueMicrotask(() => fillImages(bot.language));
  return form;
}

async function pageNewBot() {
  mount(h("a", { class: "back", href: "#/" }, "Retour aux bots"),
    h("div", { class: "pagehead" }, h("div", {}, h("h1", {}, "Nouveau bot"),
      h("p", { class: "soft" }, "Tu pourras changer tout ça plus tard dans les réglages du bot."))),
    settingsForm(null, async (data) => {
      const bot = await api("POST", "/api/bots", data);
      toast("Bot créé. Ajoute son jeton dans Variables, puis démarre-le.");
      location.hash = `#/bot/${bot.id}/variables`;
    }, "Créer le bot"));
}

// ---------------------------------------------------------------------------------------------
// Page d'un bot
// ---------------------------------------------------------------------------------------------
const TABS = [["console", "Console"], ["fichiers", "Fichiers"], ["variables", "Variables"], ["reglages", "Réglages"], ["activite", "Activité"]];

async function pageBot(id, tab) {
  let bot = await api("GET", `/api/bots/${id}`);
  const title = h("h1", {});
  const stateLine = h("p", { class: "soft" });
  const power = h("div", { class: "power" });

  function paintHead() {
    const [kind, label] = stateOf(bot);
    fill(title, lamp(kind), bot.name);
    fill(stateLine, label, " ", h("span", {}, `(${LANGS[bot.language]}, ${bot.image})`),
      bot.owner && bot.owner.id !== ME.id ? ` à ${bot.owner.name}` : "");
    const running = bot.status.state === "running";
    const doPower = (action, done) => (ev) => act(ev.currentTarget, async () => {
      bot = { ...bot, ...(await api("POST", `/api/bots/${id}/power`, { action })) };
      paintHead();
    }, done);
    fill(power, 
      running ? null : h("button", { class: "btn go", type: "button", onclick: doPower("start", "Bot démarré") }, "Démarrer"),
      running ? h("button", { class: "btn", type: "button", onclick: doPower("restart", "Bot redémarré") }, "Redémarrer") : null,
      running ? h("button", { class: "btn", type: "button", onclick: doPower("stop", "Bot arrêté") }, "Arrêter") : null,
      running ? h("button", { class: "btn danger", type: "button", title: "Arrêt immédiat, sans laisser le bot se fermer proprement", onclick: doPower("kill", "Bot tué") }, "Tuer") : null);
  }
  paintHead();

  const tabs = h("nav", { class: "tabs", "aria-label": "Onglets du bot" },
    TABS.map(([key, label]) => h("a", { href: `#/bot/${id}/${key}`, class: key === tab ? "current" : null, "aria-current": key === tab ? "page" : null }, label)));
  const body = h("div", {});
  mount(h("a", { class: "back", href: "#/" }, "Retour aux bots"),
    h("div", { class: "bothead" }, h("div", {}, title, stateLine), power), tabs, body);

  const setBot = (fresh) => { bot = { ...bot, ...fresh }; paintHead(); };
  if (tab === "console") tabConsole(bot, body, setBot);
  else if (tab === "fichiers") await tabFiles(bot, body);
  else if (tab === "variables") await tabEnv(bot, body);
  else if (tab === "reglages") tabSettings(bot, body, setBot);
  else if (tab === "activite") await tabActivity(bot, body);
}

// ---- Console -----------------------------------------------------------------------------------
const ANSI = /\x1b\[[0-9;?]*[ -\/]*[@-~]/g;

function tabConsole(bot, body, setBot) {
  const screen = h("div", { class: "screen", role: "log", "aria-live": "off", "aria-label": "Console du bot" });
  const input = h("input", { type: "text", placeholder: "Envoyer une ligne au bot (entrée standard)", "aria-label": "Commande", autocomplete: "off", spellcheck: "false" });
  const gauges = h("dl", { class: "gauges" });
  body.append(h("div", { class: "consolegrid" },
    h("div", { class: "tube" }, screen, h("form", { class: "prompt" }, h("span", { "aria-hidden": "true" }, ">"), input)),
    gauges));

  function addLine(text) {
    const stick = screen.scrollHeight - screen.scrollTop - screen.clientHeight < 40;
    const clean = text.replace(ANSI, "");
    screen.append(h("div", { class: clean.startsWith("[Panel]") ? "panel" : null }, clean || " "));
    while (screen.childElementCount > 1500) screen.firstElementChild.remove();
    if (stick) screen.scrollTop = screen.scrollHeight;
  }
  function paintGauges(b) {
    const st = b.stats;
    const g = (label, value, extra) => h("div", { class: "gauge" }, h("dt", {}, label), h("dd", {}, value, extra ? h("small", {}, ` ${extra}`) : null));
    fill(gauges, 
      g("Mémoire", st ? fmtMB(st.memory_mb) : "—", `sur ${fmtMB(b.ram_mb)}`),
      g("CPU", st ? `${st.cpu_percent} %` : "—", `max ${b.cpu_percent} %`),
      g("Disque", fmtMB(b.disk_mb)),
      g("En marche depuis", b.status.state === "running" && b.status.started_at ? since(b.status.started_at) : "—"),
      b.web_url ? h("div", { class: "gauge" }, h("dt", {}, "Adresse web"),
        h("dd", { class: "small" }, h("a", { href: b.web_url, target: "_blank", rel: "noopener" }, b.web_url))) : null);
  }
  paintGauges(bot);

  const history = [];
  let hpos = 0;
  input.closest("form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const text = input.value;
    if (!text.trim() || !ws || ws.readyState !== 1) return;
    ws.send(JSON.stringify({ type: "command", text }));
    addLine(`> ${text}`);
    history.push(text); hpos = history.length;
    input.value = "";
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowUp" && hpos > 0) { input.value = history[--hpos]; ev.preventDefault(); }
    if (ev.key === "ArrowDown") { hpos = Math.min(history.length, hpos + 1); input.value = history[hpos] || ""; ev.preventDefault(); }
  });

  let ws, closed = false, retry = 0;
  function connect() {
    ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/bots/${bot.id}/console`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "history") {
        fill(screen);
        if (!msg.lines.length) screen.append(h("div", { class: "muted" }, "Rien encore. Démarre le bot pour voir ce qu'il écrit ici."));
        msg.lines.forEach(addLine);
        screen.scrollTop = screen.scrollHeight;
      } else if (msg.type === "line") {
        const hint = screen.querySelector(".muted");
        if (hint) hint.remove();
        addLine(msg.text);
      } else if (msg.type === "status") { setBot(msg.bot); paintGauges(msg.bot); }
      retry = 0;
    };
    ws.onclose = () => {
      if (closed) return;
      addLine("[Panel] Connexion à la console perdue, reconnexion…");
      setTimeout(connect, Math.min(10000, 1000 * 2 ** retry++));
    };
  }
  connect();
  onLeave(() => { closed = true; ws.close(); });
}

function since(iso) {
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} j ${Math.floor((s % 86400) / 3600)} h`;
}

// ---- Fichiers ----------------------------------------------------------------------------------
const ICONS = {
  dir: "M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2h9A1.5 1.5 0 0 1 21 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 17.5z",
  file: "M6 3h8l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zm8 0v5h5",
};
function icon(kind) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", "ficon"); svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
  p.setAttribute("d", ICONS[kind]);
  p.setAttribute("fill", kind === "dir" ? "#C9C2F2" : "none");
  p.setAttribute("stroke", kind === "dir" ? "#4A35C8" : "#5C5978");
  p.setAttribute("stroke-width", "1.6"); p.setAttribute("stroke-linejoin", "round");
  svg.append(p);
  return svg;
}

async function tabFiles(bot, body) {
  let path = sessionStorage.getItem(`path:${bot.id}`) || "";
  const base = `/api/bots/${bot.id}/files`;

  async function show() {
    sessionStorage.setItem(`path:${bot.id}`, path);
    let data;
    try { data = await api("GET", `${base}?path=${encodeURIComponent(path)}`); }
    catch (e) { path = ""; data = await api("GET", base); }
    const crumbs = h("div", { class: "crumbs" }, h("button", { type: "button", onclick: () => { path = ""; show(); } }, "Dossier du bot"));
    const parts = path ? path.split("/") : [];
    parts.forEach((part, i) => crumbs.append(h("span", { "aria-hidden": "true" }, "/"),
      h("button", { type: "button", onclick: () => { path = parts.slice(0, i + 1).join("/"); show(); } }, part)));
    const upload = h("input", { type: "file", multiple: true, hidden: true, onchange: (e) => sendFiles(e.target.files) });
    const bar = h("div", { class: "filebar" }, crumbs,
      h("button", { class: "btn small", type: "button", onclick: newFile }, "Nouveau fichier"),
      h("button", { class: "btn small", type: "button", onclick: newDir }, "Nouveau dossier"),
      h("button", { class: "btn small", type: "button", onclick: () => upload.click() }, "Téléverser"), upload,
      bot.git_url ? h("button", { class: "btn small", type: "button", onclick: (e) => act(e.currentTarget, async () => {
        const r = await api("POST", `/api/bots/${bot.id}/git`);
        toast(r.message + (bot.status.state === "running" ? " Redémarre le bot pour l'appliquer." : ""));
        show();
      }) }, "Mettre à jour depuis Git") : null);
    const rows = data.entries.map((e) => {
      const open = () => (e.dir ? (path = e.path, show()) : edit(e.path));
      const actions = h("div", { class: "factions" },
        !e.dir ? h("a", { class: "btn small", href: `${base}/download?path=${encodeURIComponent(e.path)}` }, "Télécharger") : null,
        /\.zip$/i.test(e.name) ? h("button", { class: "btn small", type: "button", onclick: (ev) => act(ev.currentTarget, async () => {
          const r = await api("POST", `${base}/extract`, { path: e.path }); toast(`${r.count} fichier(s) décompressé(s)`); show();
        }) }, "Décompresser") : null,
        h("button", { class: "btn small", type: "button", onclick: (ev) => {
          const to = prompt("Nouveau nom (ou chemin) :", e.path);
          if (to && to !== e.path) act(ev.currentTarget, () => api("POST", `${base}/rename`, { from: e.path, to }), "Renommé").then(show);
        } }, "Renommer"),
        h("button", { class: "btn small danger", type: "button", onclick: (ev) => {
          if (confirm(`Supprimer « ${e.name} »${e.dir ? " et tout ce qu'il contient" : ""} ?`))
            act(ev.currentTarget, () => api("POST", `${base}/delete`, { paths: [e.path] }), "Supprimé").then(show);
        } }, "Supprimer"));
      return h("tr", { class: e.hidden ? "dim" : null },
        h("td", {}, h("button", { class: "fname", type: "button", onclick: open }, icon(e.dir ? "dir" : "file"), e.name)),
        h("td", { class: "num hide-sm" }, e.dir ? "" : fmtBytes(e.size)),
        h("td", { class: "num hide-sm" }, fmtDate(e.modified)),
        h("td", {}, actions));
    });
    const table = rows.length
      ? h("table", { class: "filelist" }, h("thead", {}, h("tr", {}, h("th", {}, "Nom"), h("th", { class: "num hide-sm" }, "Taille"),
        h("th", { class: "num hide-sm" }, "Modifié"), h("th", {}, ""))), h("tbody", {}, rows))
      : h("div", { class: "rack" }, h("div", { class: "empty" }, h("h2", {}, "Dossier vide"),
        h("p", { class: "soft" }, "Glisse tes fichiers ici, ou téléverse un .zip de ton bot puis décompresse-le.")));
    const zone = h("div", {}, bar, table,
      h("p", { class: "soft small", style: null }, "Astuce : glisse des fichiers sur la liste pour les téléverser dans ce dossier."));
    zone.addEventListener("dragover", (ev) => { ev.preventDefault(); zone.classList.add("drop"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drop"));
    zone.addEventListener("drop", (ev) => { ev.preventDefault(); zone.classList.remove("drop"); sendFiles(ev.dataTransfer.files); });
    fill(body, zone);
  }

  async function sendFiles(list) {
    if (!list || !list.length) return;
    const fd = new FormData();
    fd.append("path", path);
    for (const f of list) fd.append("files", f, f.name);
    toast(`Envoi de ${list.length} fichier(s)…`);
    await act(null, async () => { const r = await api("POST", `${base}/upload`, fd); toast(`${r.saved.length} fichier(s) téléversé(s)`); });
    show();
  }
  function newFile() {
    const name = prompt("Nom du nouveau fichier :", "nouveau.py");
    if (name) edit(path ? `${path}/${name}` : name, true);
  }
  function newDir() {
    const name = prompt("Nom du nouveau dossier :");
    if (name) act(null, () => api("POST", `${base}/mkdir`, { path: path ? `${path}/${name}` : name })).then(show);
  }
  async function edit(file, isNew = false) {
    let content = "";
    if (!isNew) {
      try { content = (await api("GET", `${base}/content?path=${encodeURIComponent(file)}`)).content; }
      catch (e) { toast(e.message, true); return; }
    }
    const area = h("textarea", { class: "editor", spellcheck: "false", "aria-label": `Contenu de ${file}` });
    area.value = content;
    const save = h("button", { class: "btn primary", type: "button" }, "Enregistrer");
    const doSave = () => act(save, () => api("PUT", `${base}/content`, { path: file, content: area.value }), "Fichier enregistré");
    save.addEventListener("click", doSave);
    area.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === "s") { ev.preventDefault(); doSave(); }
      if (ev.key === "Tab" && !ev.shiftKey) { ev.preventDefault(); area.setRangeText("    ", area.selectionStart, area.selectionEnd, "end"); }
    });
    fill(body, 
      h("div", { class: "filebar" }, h("div", { class: "crumbs" }, h("strong", {}, file)), save,
        h("button", { class: "btn", type: "button", onclick: show }, "Fermer")),
      area, h("p", { class: "soft small" }, "Ctrl+S pour enregistrer. Redémarre le bot pour qu'il prenne les changements."));
    area.focus();
  }
  await show();
}

// ---- Variables ---------------------------------------------------------------------------------
async function tabEnv(bot, body) {
  const vars = await api("GET", `/api/bots/${bot.id}/env`);
  const list = h("div", {});
  function row(v = { key: "", value: "", secret: false, set: false }) {
    const key = h("input", { type: "text", name: "key", value: v.key, placeholder: "NOM", "aria-label": "Nom de la variable", autocomplete: "off", spellcheck: "false" });
    const value = h("input", { type: v.secret ? "password" : "text", name: "value", value: v.secret ? "" : v.value,
      placeholder: v.secret && v.set ? "Valeur gardée (écris pour la changer)" : "valeur", "aria-label": "Valeur", autocomplete: "off" });
    const secret = h("input", { type: "checkbox", checked: v.secret, onchange: (e) => { value.type = e.target.checked ? "password" : "text"; } });
    const el = h("div", { class: "envrow" }, key, value, h("label", { class: "check" }, secret, "Secret"),
      h("button", { class: "btn small danger", type: "button", onclick: () => el.remove() }, "Retirer"));
    el.read = () => ({ key: key.value.trim(), secret: secret.checked, value: secret.checked && v.set && v.secret && !value.value ? null : value.value });
    return el;
  }
  vars.forEach((v) => list.append(row(v)));
  if (!vars.length) list.append(row({ key: "DISCORD_TOKEN", value: "", secret: true, set: false }));
  const save = h("button", { class: "btn primary", type: "button" }, "Enregistrer les variables");
  save.addEventListener("click", () => act(save, async () => {
    const items = [...list.children].map((r) => r.read()).filter((v) => v.key);
    const r = await api("PUT", `/api/bots/${bot.id}/env`, { variables: items });
    toast(r.restart_needed ? "Variables enregistrées. Redémarre le bot pour qu'il les voie." : "Variables enregistrées");
    tabEnv(bot, fill(body) || body);
  }));
  fill(body, h("div", { class: "panelbox" },
    h("h2", {}, "Variables d'environnement"),
    h("p", { class: "soft" }, "Ton bot les lit avec ", h("code", {}, "os.getenv(\"NOM\")"), " en Python ou ", h("code", {}, "process.env.NOM"),
      " en Node.js. Mets le jeton de ton bot dans ", h("code", {}, "DISCORD_TOKEN"), " : il est caché après l'enregistrement et le panel le montre plus jamais. Partage-le à personne."),
    list,
    h("div", { class: "formactions" }, h("button", { class: "btn", type: "button", onclick: () => list.append(row()) }, "Ajouter une variable"), save)));
}

// ---- Réglages ----------------------------------------------------------------------------------
function tabSettings(bot, body, setBot) {
  const general = settingsForm(bot, async (data) => {
    const fresh = await api("PATCH", `/api/bots/${bot.id}`, data);
    setBot(fresh); Object.assign(bot, fresh);
    toast(bot.status.state === "running" ? "Réglages enregistrés. Redémarre le bot pour les appliquer." : "Réglages enregistrés");
  }, "Enregistrer les réglages");
  general.prepend(h("h2", {}, "Réglages"), h("p", { class: "soft" }, "Les changements s'appliquent au prochain démarrage du bot."));

  const gitForm = h("form", { class: "panelbox" },
    h("h2", {}, "Déploiement GitHub"),
    h("p", { class: "soft" }, "Le panel clone ton dépôt dans le dossier du bot. « Mettre à jour depuis Git » (onglet Fichiers) récupère les derniers changements. Tes fichiers à part, comme .env, sont gardés."),
    h("div", { class: "field" }, h("label", { for: "g-url" }, "Adresse du dépôt"),
      h("input", { type: "url", id: "g-url", name: "git_url", value: bot.git_url, placeholder: "https://github.com/toi/mon-bot" })),
    h("div", { class: "row" },
      h("div", { class: "field" }, h("label", { for: "g-branch" }, "Branche"), h("input", { type: "text", id: "g-branch", name: "git_branch", value: bot.git_branch, placeholder: "principale" })),
      h("div", { class: "field" }, h("label", { for: "g-token" }, "Jeton GitHub (dépôt privé)"),
        h("input", { type: "password", id: "g-token", name: "git_token", autocomplete: "off", placeholder: bot.has_git_token ? "Jeton gardé (écris pour le changer)" : "" }))),
    h("div", { class: "formactions" }, h("button", { class: "btn primary", type: "submit" }, "Enregistrer"),
      bot.has_git_token ? h("button", { class: "btn danger", type: "button", onclick: (e) => act(e.currentTarget, async () => {
        Object.assign(bot, await api("PATCH", `/api/bots/${bot.id}`, { git_token: "" })); tabSettings(bot, body, setBot);
      }, "Jeton retiré") }, "Retirer le jeton") : null));
  gitForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const fd = new FormData(gitForm);
    const data = { git_url: fd.get("git_url"), git_branch: fd.get("git_branch") };
    if (fd.get("git_token")) data.git_token = fd.get("git_token");
    act(ev.submitter, async () => { Object.assign(bot, await api("PATCH", `/api/bots/${bot.id}`, data)); }, "Dépôt enregistré");
  });

  const confirmInput = h("input", { type: "text", id: "del-confirm", autocomplete: "off" });
  const del = h("button", { class: "btn danger solid", type: "button", disabled: true }, "Supprimer le bot");
  confirmInput.addEventListener("input", () => { del.disabled = confirmInput.value !== bot.name; });
  del.addEventListener("click", () => act(del, async () => {
    await api("DELETE", `/api/bots/${bot.id}`, { confirm: confirmInput.value });
    toast("Bot supprimé"); location.hash = "#/";
  }));
  const danger = h("div", { class: "panelbox danger" }, h("h2", {}, "Supprimer le bot"),
    h("p", { class: "soft" }, "Arrête le bot et efface son conteneur, ses fichiers et ses variables. Ça se défait pas."),
    h("div", { class: "field" }, h("label", { for: "del-confirm" }, `Écris « ${bot.name} » pour confirmer`), confirmInput), del);
  fill(body, general, gitForm, danger);
}

// ---- Activité ----------------------------------------------------------------------------------
async function tabActivity(bot, body) {
  const items = await api("GET", `/api/bots/${bot.id}/activity`);
  fill(body, h("div", { class: "panelbox" }, h("h2", {}, "Activité"),
    items.length ? h("ul", { class: "feed" }, items.map((a) => h("li", {}, h("span", { class: "soft" }, fmtDate(a.at)),
      h("span", {}, h("strong", {}, a.action), a.detail ? ` : ${a.detail}` : "", a.user ? h("span", { class: "soft" }, ` par ${a.user}`) : ""))))
      : h("p", { class: "soft" }, "Rien encore.")));
}

// ---------------------------------------------------------------------------------------------
// Constructeur de serveurs Discord
// ---------------------------------------------------------------------------------------------
const PERMS = ["administrator", "view_channel", "send_messages", "read_message_history", "add_reactions", "embed_links",
  "attach_files", "use_external_emojis", "mention_everyone", "manage_messages", "manage_channels", "manage_roles",
  "manage_guild", "kick_members", "ban_members", "moderate_members", "manage_nicknames", "change_nickname",
  "create_instant_invite", "view_audit_log", "manage_webhooks", "manage_threads", "create_public_threads",
  "send_messages_in_threads", "use_application_commands", "connect", "speak", "stream", "mute_members",
  "deafen_members", "move_members", "priority_speaker", "use_voice_activation", "manage_events", "send_polls", "pin_messages"];

async function pageBuilder() {
  const sources = await api("GET", "/api/builder/sources");
  const templates = await api("GET", "/api/builder/templates");
  const state = { tid: null, mirror: false, plan: null };

  const botSel = h("select", { id: "b-bot" }, h("option", { value: "" }, "Choisis un de tes bots"),
    sources.map((s) => h("option", { value: s.id, disabled: !s.has_token }, s.has_token ? s.name : `${s.name} (pas de DISCORD_TOKEN)`)));
  const guildSel = h("select", { id: "b-guild", disabled: true }, h("option", { value: "" }, "Choisis d'abord un bot"));
  const inviteLine = h("p", { class: "hint" });
  const area = h("textarea", { class: "yaml", spellcheck: "false", "aria-label": "Modèle du serveur (YAML)" });
  const tplSel = h("select", { "aria-label": "Modèles enregistrés" }, h("option", { value: "" }, "Nouveau modèle"),
    templates.map((t) => h("option", { value: t.id }, t.name)));
  const planList = h("ul", { class: "plan" });
  const planHead = h("div", {});
  const planBtn = h("button", { class: "btn primary", type: "button" }, "Voir le plan");
  const applyBtn = h("button", { class: "btn go", type: "button", hidden: true }, "Appliquer");
  const confirmBox = h("label", { class: "check", hidden: true });

  const saved = localStorage.getItem("builder:draft");
  area.value = saved || (await api("POST", "/api/builder/validate", { example: true })).content;
  area.addEventListener("input", () => { localStorage.setItem("builder:draft", area.value); resetPlan(); });
  area.addEventListener("keydown", (ev) => {
    if (ev.key === "Tab" && !ev.shiftKey) { ev.preventDefault(); area.setRangeText("  ", area.selectionStart, area.selectionEnd, "end"); }
  });

  function resetPlan() {
    state.plan = null; applyBtn.hidden = true; confirmBox.hidden = true;
    fill(planHead); fill(planList);
  }

  botSel.addEventListener("change", async () => {
    resetPlan();
    guildSel.disabled = true; fill(inviteLine);
    if (!botSel.value) return;
    fill(guildSel, h("option", { value: "" }, "Chargement…"));
    try {
      const data = await api("GET", `/api/builder/sources/${botSel.value}`);
      fill(guildSel, h("option", { value: "" }, data.guilds.length ? "Choisis un serveur" : "Le bot est sur aucun serveur"),
        data.guilds.map((g) => h("option", { value: g.id }, g.name)));
      guildSel.disabled = !data.guilds.length;
      fill(inviteLine, `Bot connecté : ${data.bot.name}. `,
        h("a", { href: data.invite, target: "_blank", rel: "noopener" }, "Inviter ce bot sur un autre serveur"),
        " (avec la permission Administrateur, le temps de construire).");
    } catch (e) { toast(e.message, true); fill(guildSel, h("option", { value: "" }, "Erreur")); }
  });
  guildSel.addEventListener("change", resetPlan);

  tplSel.addEventListener("change", async () => {
    resetPlan();
    if (!tplSel.value) { state.tid = null; return; }
    const t = await api("GET", `/api/builder/templates/${tplSel.value}`);
    state.tid = t.id; area.value = t.content; localStorage.setItem("builder:draft", area.value);
  });
  const saveTpl = h("button", { class: "btn small", type: "button", onclick: (e) => {
    const current = templates.find((t) => String(t.id) === String(state.tid));
    const name = prompt("Nom du modèle :", current ? current.name : "Mon serveur");
    if (!name) return;
    act(e.currentTarget, async () => {
      const r = await api("POST", "/api/builder/templates", { id: state.tid, name, content: area.value });
      if (!current) { templates.push({ id: r.id, name }); tplSel.append(h("option", { value: r.id }, name)); }
      else { current.name = name; tplSel.querySelector(`option[value="${r.id}"]`).textContent = name; }
      state.tid = r.id; tplSel.value = String(r.id);
    }, "Modèle enregistré");
  } }, "Enregistrer");
  const delTpl = h("button", { class: "btn small danger", type: "button", onclick: (e) => {
    if (!state.tid || !confirm("Supprimer ce modèle enregistré ?")) return;
    act(e.currentTarget, async () => {
      await api("DELETE", `/api/builder/templates/${state.tid}`);
      tplSel.querySelector(`option[value="${state.tid}"]`).remove(); state.tid = null; tplSel.value = "";
    }, "Modèle supprimé");
  } }, "Supprimer");
  const exampleBtn = h("button", { class: "btn small", type: "button", onclick: async () => {
    if (!confirm("Remplacer le texte par le modèle d'exemple ?")) return;
    area.value = (await api("POST", "/api/builder/validate", { example: true })).content; area.dispatchEvent(new Event("input"));
  } }, "Exemple");
  const exportBtn = h("button", { class: "btn small", type: "button", onclick: (e) => {
    if (!botSel.value || !guildSel.value) { toast("Choisis le bot et le serveur à copier", true); return; }
    if (!confirm("Remplacer le texte par une copie du serveur choisi ?")) return;
    act(e.currentTarget, async () => {
      area.value = (await api("POST", "/api/builder/export", { bot_id: botSel.value, guild_id: guildSel.value })).content;
      area.dispatchEvent(new Event("input"));
    }, "Serveur copié dans le modèle");
  } }, "Copier le serveur choisi");
  const importBtn = h("button", { class: "btn small", type: "button", onclick: (e) => {
    if (!botSel.value) { toast("Choisis d'abord un bot (il sert à lire le modèle)", true); return; }
    const code = prompt("Lien ou code du modèle Discord (ex. https://discord.new/abcd1234) :");
    if (!code) return;
    act(e.currentTarget, async () => {
      area.value = (await api("POST", "/api/builder/import", { bot_id: botSel.value, code })).content;
      area.dispatchEvent(new Event("input"));
    }, "Modèle importé");
  } }, "Importer discord.new");

  const modes = h("div", { class: "modes", role: "radiogroup", "aria-label": "Mode" },
    h("label", { class: "check" }, h("input", { type: "radio", name: "mode", checked: true, onchange: () => { state.mirror = false; resetPlan(); } }),
      h("span", {}, "Ajouter et modifier", h("span", { class: "hint" }, "Crée ce qui manque et corrige ce qui diffère. Rien n'est supprimé."))),
    h("label", { class: "check" }, h("input", { type: "radio", name: "mode", onchange: () => { state.mirror = true; resetPlan(); } }),
      h("span", {}, "Copie exacte", h("span", { class: "hint" }, "Fait la même chose, puis supprime les rôles et salons qui sont pas dans le modèle (après confirmation)."))));

  const KIND = (a) => (a.blocked ? ["blocked", "!"] : a.kind.startsWith("create") ? ["create", "+"] : a.kind.startsWith("delete") ? ["delete", "−"] : ["update", "~"]);
  planBtn.addEventListener("click", () => {
    if (!botSel.value || !guildSel.value) { toast("Choisis le bot et le serveur", true); return; }
    act(planBtn, async () => {
      const plan = await api("POST", "/api/builder/plan", { bot_id: botSel.value, guild_id: guildSel.value, content: area.value, mirror: state.mirror });
      state.plan = plan;
      const c = plan.counts;
      fill(planHead, 
        h("div", { class: "counts" }, h("span", {}, h("b", {}, c.create), " à créer"), h("span", {}, h("b", {}, c.update), " à modifier"),
          h("span", {}, h("b", {}, c.delete), " à supprimer"), c.blocked ? h("span", {}, h("b", {}, c.blocked), " impossible") : null),
        plan.warnings.map((w) => h("p", { class: "note" }, w)));
      fill(planList, ...(plan.actions.length ? plan.actions.map((a) => {
        const [cls, mark] = KIND(a);
        return h("li", { class: cls }, h("span", { class: "mark", "aria-hidden": "true" }, mark), h("span", {}, a.label, a.blocked ? h("small", {}, a.blocked) : null));
      }) : [h("li", {}, h("span", { class: "mark" }, "✓"), h("span", {}, `« ${plan.guild} » correspond déjà au modèle.`))]));
      const doable = plan.actions.filter((a) => !a.blocked);
      applyBtn.hidden = !doable.length;
      applyBtn.textContent = `Appliquer à « ${plan.guild} »`;
      confirmBox.hidden = !c.delete;
      fill(confirmBox, h("input", { type: "checkbox", id: "b-confirm" }), `Je confirme la suppression de ${c.delete} élément(s)`);
    });
  });
  applyBtn.addEventListener("click", () => act(applyBtn, async () => {
    const confirmDelete = !confirmBox.hidden && document.getElementById("b-confirm").checked;
    let job = await api("POST", "/api/builder/apply", { bot_id: botSel.value, guild_id: guildSel.value, content: area.value, mirror: state.mirror, confirm_delete: confirmDelete });
    applyBtn.hidden = true; confirmBox.hidden = true;
    const paint = () => {
      fill(planHead, h("div", { class: "counts" }, h("span", {}, h("b", {}, job.done), ` étape(s) sur ${job.total}`)),
        job.status === "done" ? h("p", { class: "note" }, "Terminé. Va voir ton serveur !") : null,
        job.error ? h("p", { class: "note bad" }, job.error) : null);
      const marks = { ok: "✓", error: "✗", skipped: "–", running: "…", pending: " " };
      fill(planList, ...job.steps.map((s) => h("li", { class: s.state }, h("span", { class: "mark", "aria-hidden": "true" }, marks[s.state]),
        h("span", {}, s.label, s.error ? h("small", {}, s.error) : null))));
    };
    paint();
    while (job.status === "running") {
      await new Promise((r) => setTimeout(r, 900));
      job = await api("GET", `/api/builder/jobs/${job.id}`);
      paint();
    }
  }));

  mount(
    h("div", { class: "pagehead" }, h("div", {}, h("h1", {}, "Serveurs Discord"),
      h("p", { class: "soft" }, "Décris les rôles, catégories et salons dans un modèle, regarde le plan, puis applique-le sur un serveur avec un de tes bots."))),
    sources.some((s) => s.has_token) ? null : h("div", { class: "banner" }, "Il te faut un bot avec la variable DISCORD_TOKEN : c'est lui qui construit le serveur. ",
      h("a", { href: "#/" }, "Va dans tes bots"), ", onglet Variables."),
    h("div", { class: "target" },
      h("div", {}, h("label", { for: "b-bot" }, "Bot qui construit"), botSel),
      h("div", {}, h("label", { for: "b-guild" }, "Serveur"), guildSel, inviteLine)),
    h("div", { class: "buildgrid" },
      h("div", {},
        h("div", { class: "tpl-tools" }, tplSel, saveTpl, delTpl),
        h("div", { class: "tpl-tools" }, exampleBtn, exportBtn, importBtn),
        area,
        h("details", { class: "help" }, h("summary", {}, "Comment écrire un modèle"),
          h("p", { class: "soft small" }, "Les rôles vont du plus haut au plus bas. Une catégorie avec ", h("code", {}, "private: true"),
            " est cachée à @everyone. Les salons reprennent les permissions de leur catégorie ; ce que tu mets dans un salon remplace celles de la catégorie pour ce rôle (", h("code", {}, "Rôle: {}"), " = rien de spécial). Les rôles des autres bots et les permissions par membre sont jamais touchés."),
          h("pre", {}, "roles:\n  - name: Modo\n    color: \"#9b59b6\"\n    hoist: true\n    permissions: [manage_messages, kick_members]\ncategories:\n  - name: Staff\n    private: true\n    overwrites:\n      Modo: {allow: [view_channel]}\n    channels:\n      - name: staff\n      - {name: Réunion, type: voice, user_limit: 10}"),
          h("p", { class: "soft small" }, "Types de salon : text, voice, announcement, stage, forum. Options : topic, slowmode (secondes), nsfw, user_limit (vocal)."),
          h("p", { class: "soft small" }, "Permissions :"), h("ul", { class: "permlist" }, PERMS.map((p) => h("li", {}, p))))),
      h("div", { class: "planbox" }, h("h2", {}, "Plan"), modes, planBtn, planHead, planList, confirmBox,
        h("div", { class: "formactions" }, applyBtn))));
}

// ---------------------------------------------------------------------------------------------
// Admin
// ---------------------------------------------------------------------------------------------
async function pageAdmin() {
  const [users, sys] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/system")]);
  const ROLES = { admin: "Admin", user: "Utilisateur", pending: "En attente", blocked: "Bloqué" };
  const rows = users.map((u) => {
    const role = h("select", { "aria-label": `Rôle de ${u.name}`, disabled: u.from_env || u.id === ME.id },
      Object.entries(ROLES).map(([v, l]) => h("option", { value: v }, l)));
    role.value = u.role;
    const num = (name, value, label) => h("input", { type: "number", min: 0, value, "aria-label": `${label} de ${u.name}`, dataset: { k: name } });
    const lims = [num("max_bots", u.max_bots, "Bots max"), num("max_ram_mb", u.max_ram_mb, "RAM max"), num("max_disk_mb", u.max_disk_mb, "Disque max")];
    const save = h("button", { class: "btn small", type: "button" }, "Enregistrer");
    save.addEventListener("click", () => act(save, async () => {
      const data = { role: role.value };
      lims.forEach((i) => { data[i.dataset.k] = Number(i.value); });
      if (u.from_env || u.id === ME.id) delete data.role;
      await api("PATCH", `/api/admin/users/${u.id}`, data);
    }, "Compte enregistré").then(() => { if (role.value !== u.role) route(); }));
    const approve = u.role === "pending" ? h("button", { class: "btn small go", type: "button", onclick: (e) => act(e.currentTarget,
      () => api("PATCH", `/api/admin/users/${u.id}`, { role: "user" }), `${u.name} approuvé`).then(route) }, "Approuver") : null;
    return h("tr", { class: u.role === "pending" ? "pending" : null },
      h("td", {}, h("div", { class: "who" }, h("img", { class: "avatar", src: u.avatar, alt: "" }),
        h("div", {}, h("strong", {}, u.name), h("div", { class: "soft small" }, u.id)))),
      h("td", {}, role, u.from_env ? h("div", { class: "hint" }, "Admin dans .env") : null),
      lims.map((i) => h("td", {}, i)),
      h("td", { class: "soft small" }, `${u.usage.bots} bot(s)`, h("br"), `${fmtMB(u.usage.ram_mb)} RAM, ${fmtMB(u.usage.disk_mb)} disque`),
      h("td", {}, h("div", { class: "factions" }, approve, save)));
  });
  const idInput = h("input", { type: "text", id: "add-id", placeholder: "ID Discord", inputmode: "numeric", autocomplete: "off" });
  const addForm = h("form", { class: "formactions" }, h("div", { class: "field", style: null }, idInput),
    h("button", { class: "btn", type: "submit" }, "Ajouter un ami"));
  addForm.addEventListener("submit", (ev) => {
    ev.preventDefault();
    act(ev.submitter, () => api("POST", "/api/admin/users", { id: idInput.value.trim() }), "Compte ajouté : il peut se connecter").then(route);
  });
  const pending = users.filter((u) => u.role === "pending").length;
  mount(
    h("div", { class: "pagehead" }, h("div", {}, h("h1", {}, "Admin"),
      h("p", { class: "soft" }, pending ? `${pending} compte(s) attendent ton approbation.` : "Approuve les comptes de tes amis et choisis leurs limites."))),
    sys.docker ? null : h("div", { class: "banner" }, `Docker : ${sys.docker_error}`),
    h("dl", { class: "sysline" },
      h("div", {}, h("dt", {}, "Bots en marche"), h("dd", {}, `${sys.running} sur ${sys.bots}`)),
      h("div", {}, h("dt", {}, "Docker"), h("dd", {}, sys.docker_version || "injoignable")),
      h("div", {}, h("dt", {}, "Version du panel"), h("dd", {}, sys.version)),
      h("div", {}, h("dt", {}, "Dossier des bots sur la machine"), h("dd", { class: "small" }, `${sys.host_data_dir}/bots`))),
    h("h2", { class: "section-gap" }, "Comptes"),
    h("p", { class: "soft small" }, "Limites : nombre de bots, RAM totale réservée (Mo) et espace disque total (Mo). L'admin a pas de limite."),
    h("div", { class: "users-wrap" }, h("table", { class: "users" },
      h("thead", {}, h("tr", {}, ["Compte", "Rôle", "Bots", "RAM (Mo)", "Disque (Mo)", "Utilisé", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows))),
    h("h3", { class: "section-gap" }, "Ajouter un ami d'avance"),
    h("p", { class: "soft small" }, "Avec son ID Discord (dans Discord : mode développeur, clic droit sur son profil, Copier l'identifiant). Il pourra se connecter tout de suite."),
    addForm,
    h("h2", { class: "section-gap" }, "Activité récente"),
    h("ul", { class: "feed" }, sys.activity.map((a) => h("li", {}, h("span", { class: "soft" }, fmtDate(a.at)),
      h("span", {}, h("strong", {}, a.action), a.bot ? ` (${a.bot})` : "", a.detail ? ` : ${a.detail}` : "", a.user ? h("span", { class: "soft" }, ` par ${a.user}`) : "")))));
}

// ---------------------------------------------------------------------------------------------
// Démarrage
// ---------------------------------------------------------------------------------------------
async function boot() {
  ME = await api("GET", "/api/me");
  document.getElementById("me-avatar").src = ME.avatar;
  document.getElementById("me-name").textContent = ME.name;
  document.querySelector('[data-nav="admin"]').hidden = ME.role !== "admin";
  document.getElementById("logout").addEventListener("click", async () => {
    await api("POST", "/api/logout"); location.href = "/login";
  });
  window.addEventListener("hashchange", route);
  route();
}
boot();
