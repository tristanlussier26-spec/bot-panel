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
    const nav = section === "bot" || section === "nouveau" || !section ? "bots" : section === "partage" ? "partages" : section;
    a.classList.toggle("current", a.dataset.nav === nav);
  });
  mount(h("p", { class: "loading" }, "Chargement…"));
  try {
    if (!section) await pageBots();
    else if (section === "nouveau") await pageNewBot();
    else if (section === "bot" && parts[1]) await pageBot(parts[1], parts[2] || "console");
    else if (section === "partages") await pageShared();
    else if (section === "partage" && parts[1]) await pageSharedOptions(parts[1]);
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
function settingsForm(bot, onSubmit, submitLabel, kits = []) {
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

  // Modèle de bot (nouveau bot seulement) : il fixe le langage et le fichier principal
  const kitSelect = h("select", { name: "kit", id: "f-kit" },
    kits.map((k) => h("option", { value: k.id }, `${k.name} (${k.version})`)),
    h("option", { value: "" }, "Aucun : mes propres fichiers"));
  const kitHint = h("span", { class: "hint" });
  function applyKit() {
    const kit = kits.find((k) => k.id === kitSelect.value);
    form.querySelectorAll("input[name=language]").forEach((r) => {
      r.disabled = !!kit;
      if (kit) r.checked = r.value === kit.language;
    });
    mainInput.disabled = !!kit;
    if (kit) { mainInput.value = kit.main_file; fillImages(kit.language); }
    // Le modèle décide du langage, du fichier principal et de la commande : on cache ces champs
    langs.closest(".field").hidden = !!kit;
    mainInput.closest(".field").hidden = !!kit;
    cmdField.hidden = !!kit;
    fill(kitHint, kit ? [kit.description, kit.plugins.length ? ` Plugins installés d'office : ${kit.plugins.join(", ")}.` : ""] :
      "Tu mets tes propres fichiers (ou un petit bot d'exemple est créé).");
  }
  kitSelect.addEventListener("change", applyKit);

  form.append(
    isNew && kits.length ? h("div", { class: "field" }, h("label", { for: "f-kit" }, "Modèle de bot"), kitSelect, kitHint) : null,
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
      h("input", { type: "checkbox", name: "shared", checked: !!bot.shared }),
      h("span", {}, "Bot partagé",
        h("span", { class: "hint" }, "Les comptes approuvés le voient dans « Bots partagés » : ils peuvent l'inviter sur leurs serveurs et régler ses options, seulement sur les serveurs où ils ont « Gérer le serveur ». Ils voient pas sa console, ses fichiers ni ses variables.")))),
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
      auto_restart: fd.get("auto_restart") === "on", web_port: fd.get("web_port") === "on", shared: fd.get("shared") === "on",
    };
    if (isNew && fd.get("git_url")) Object.assign(data, { git_url: fd.get("git_url"), git_branch: fd.get("git_branch"), git_token: fd.get("git_token") || null });
    if (isNew && kits.length && kitSelect.value) {
      const kit = kits.find((k) => k.id === kitSelect.value);
      Object.assign(data, { kit: kit.id, language: kit.language, main_file: kit.main_file, startup_cmd: "" });
    }
    act(submit, () => onSubmit(data));
  });
  queueMicrotask(() => { fillImages(bot.language); if (isNew && kits.length) applyKit(); });
  return form;
}

async function pageNewBot() {
  const kits = await api("GET", "/api/kits");
  mount(h("a", { class: "back", href: "#/" }, "Retour aux bots"),
    h("div", { class: "pagehead" }, h("div", {}, h("h1", {}, "Nouveau bot"),
      h("p", { class: "soft" }, "Tu pourras changer tout ça plus tard dans les réglages du bot."))),
    settingsForm(null, async (data) => {
      const bot = await api("POST", "/api/bots", data);
      (bot.notes || []).forEach((n) => console.warn(n));
      toast(bot.notes && bot.notes.length ? bot.notes[0] : "Bot créé. Ajoute son jeton dans Variables, puis démarre-le.", !!(bot.notes && bot.notes.length));
      location.hash = `#/bot/${bot.id}/variables`;
    }, "Créer le bot", kits));
}

// ---------------------------------------------------------------------------------------------
// Page d'un bot
// ---------------------------------------------------------------------------------------------
const TABS = [["console", "Console"], ["options", "Options du bot"], ["plugins", "Plugins"], ["fichiers", "Fichiers"],
  ["variables", "Variables"], ["reglages", "Réglages"], ["activite", "Activité"]];

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
    TABS.filter(([key]) => key !== "options" || bot.has_options || tab === "options").map(([key, label]) => h("a", { href: `#/bot/${id}/${key}`, class: key === tab ? "current" : null, "aria-current": key === tab ? "page" : null }, label)));
  const body = h("div", {});
  mount(h("a", { class: "back", href: "#/" }, "Retour aux bots"),
    h("div", { class: "bothead" }, h("div", {}, title, stateLine), power), tabs, body);

  const setBot = (fresh) => { bot = { ...bot, ...fresh }; paintHead(); };
  if (tab === "console") tabConsole(bot, body, setBot);
  else if (tab === "options") await tabOptions(bot, body);
  else if (tab === "plugins") await tabPlugins(bot, body);
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

// ---- Options du bot (réglages décrits par le bot) -----------------------------------------------
async function tabOptions(bot, body, onData, notice) {
  const data = await api("GET", `/api/bots/${bot.id}/options`);
  if (onData) onData(data);
  const inviteBtn = data.invite ? h("a", { class: "btn", href: data.invite, target: "_blank", rel: "noopener" }, "Inviter sur un serveur") : null;
  if (!data.available) {
    fill(body, h("div", { class: "panelbox" }, h("h2", {}, "Ce bot a pas d'options"),
      h("p", { class: "soft" }, "Un bot peut décrire ses réglages dans le fichier .botpanel/schema.json : Bot Panel affiche alors ici un formulaire pour les changer. Le README explique comment.")));
    return;
  }
  const scopes = data.scopes;
  let scopeId = sessionStorage.getItem(`scope:${bot.id}`);
  if (!scopes.some((sc) => sc.id === scopeId)) scopeId = scopes.length ? scopes[0].id : null;
  let pageId = sessionStorage.getItem(`page:${bot.id}`);
  if (!data.pages.some((p) => p.id === pageId)) pageId = data.pages.length ? data.pages[0].id : null;

  const warn = !data.running ? "Le bot est arrêté : démarre-le pour changer ses options (c'est lui qui les vérifie et les applique)."
    : data.stale ? "Le bot a pas donné de nouvelles depuis un moment : les salons et rôles affichés sont peut-être plus à jour." : null;

  if (data.scope === "guild" && !scopes.length) {
    fill(body, warn ? h("div", { class: "banner" }, warn) : null, h("div", { class: "panelbox" }, h("h2", {}, "Aucun serveur"),
      h("p", { class: "soft" }, data.full
        ? "Le bot est sur aucun serveur Discord pour l'instant, ou il a pas encore démarré. Invite-le sur un serveur, puis reviens ici."
        : "Le bot est sur aucun serveur que tu gères. Invite-le sur ton serveur (il faut la permission « Gérer le serveur »), puis reviens ici : ça prend environ 30 secondes."),
      inviteBtn ? h("div", { class: "formactions" }, inviteBtn) : null));
    return;
  }

  function render() {
    const scope = scopes.find((sc) => sc.id === scopeId);
    const page = data.pages.find((p) => p.id === pageId);
    const scopeSel = data.scope === "guild" ? h("select", { id: "opt-scope", onchange: (e) => {
      scopeId = e.target.value; sessionStorage.setItem(`scope:${bot.id}`, scopeId); render();
    } }, scopes.map((sc) => h("option", { value: sc.id }, sc.name))) : null;
    if (scopeSel) scopeSel.value = scopeId;
    const nav = h("nav", { class: "optnav", "aria-label": "Pages d'options" }, data.pages.map((p) => h("button", {
      type: "button", class: p.id === pageId ? "current" : null, "aria-current": p.id === pageId ? "page" : null,
      onclick: () => { pageId = p.id; sessionStorage.setItem(`page:${bot.id}`, pageId); render(); },
    }, p.icon ? h("span", { "aria-hidden": "true" }, p.icon) : null, p.title)));
    const fileInput = h("input", { type: "file", accept: ".json,application/json", hidden: true });
    const importBtn = h("button", { class: "btn small", type: "button", disabled: !data.running, onclick: () => fileInput.click() }, "Importer");
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files[0];
      fileInput.value = "";
      if (!file || !scope) return;
      let parsed;
      try { parsed = JSON.parse(await file.text()); } catch { toast("Ce fichier est pas un fichier d'options valide", true); return; }
      if (!confirm(`Remplacer les options de « ${scope.name} » par celles du fichier ?`)) return;
      const result = await act(importBtn, () => importOptions(bot, data, scope, parsed));
      if (!result) return;
      await new Promise((r) => setTimeout(r, 1500));  // le temps que le bot réécrive son état
      const lines = [`${result.done} page(s) importée(s) sur « ${scope.name} ».`];
      if (result.missing.length) lines.push(`Introuvables ici, laissés vides : ${result.missing.join(", ")}.`);
      result.errors.forEach((e) => lines.push(e));
      tabOptions(bot, body, onData, { text: lines.join(" "), bad: result.errors.length > 0 });
    });
    fill(body,
      warn ? h("div", { class: "banner" }, warn) : null,
      notice ? h("p", { class: `note ${notice.bad ? "bad" : ""}`, role: "status" }, notice.text) : null,
      h("div", { class: "optgrid" },
        h("div", {},
          scopeSel ? h("div", { class: "field" }, h("label", { for: "opt-scope" }, "Serveur Discord"), scopeSel) : null,
          inviteBtn ? h("div", { class: "field" }, inviteBtn) : null,
          scope && scope.info.length ? h("dl", { class: "optinfo" }, scope.info.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, v)))) : null,
          nav,
          scope ? h("div", { class: "optfile" },
            h("p", { class: "small" }, h("strong", {}, "Fichier de configuration"),
              h("span", { class: "hint" }, "Exporte les options de ce serveur, ou importe celles d'un autre serveur. Les salons et les rôles sont retrouvés par leur nom.")),
            h("div", { class: "formactions" },
              h("button", { class: "btn small", type: "button", onclick: () => exportOptions(data, scope) }, "Exporter"), importBtn, fileInput)) : null),
        page && scope ? optionsForm(bot, data, scope, page, data.running) : h("p", { class: "soft" }, "Rien à régler.")));
  }
  render();
}

// ---- Fichier de configuration : exporter / importer les options d'un serveur -------------------
const CHANNEL_TYPES = ["channel", "category", "channels"];
const ROLE_TYPES = ["role", "roles", "role_map", "number_roles"];

function refIds(f, v) {
  if (v === null || v === undefined) return [];
  if (["channel", "category", "role"].includes(f.type)) return v && String(v) !== "0" ? [String(v)] : [];
  if (["channels", "roles"].includes(f.type)) return (Array.isArray(v) ? v : []).map(String);
  if (["role_map", "number_roles"].includes(f.type)) return Object.values(v || {}).map(String).filter((x) => x !== "0");
  return [];
}

function exportOptions(data, scope) {
  const chanName = new Map([...scope.channels, ...scope.categories].map((c) => [c.id, c.name]));
  const roleName = new Map(scope.roles.map((r) => [r.id, r.name]));
  const names = { channels: {}, roles: {} };
  const pages = {};
  for (const p of data.pages) {
    const vals = scope.values[p.id];
    if (!vals) continue;
    pages[p.id] = {};
    for (const f of p.fields) {
      if (!(f.key in vals)) continue;
      pages[p.id][f.key] = vals[f.key];
      for (const id of refIds(f, vals[f.key])) {
        if (CHANNEL_TYPES.includes(f.type) && chanName.has(id)) names.channels[id] = chanName.get(id);
        if (ROLE_TYPES.includes(f.type) && roleName.has(id)) names.roles[id] = roleName.get(id);
      }
    }
  }
  const file = { format: "botpanel-options", version: 1, bot: data.bot_name || data.title, server: scope.name,
    exported: new Date().toISOString(), pages, names };
  const blob = new Blob([JSON.stringify(file, null, 2)], { type: "application/json" });
  const a = h("a", { href: URL.createObjectURL(blob), download: `options-${scope.name.replace(/[^\p{L}\p{N}]+/gu, "-").toLowerCase()}.json` });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  toast("Fichier d'options téléchargé");
}

async function importOptions(bot, data, scope, file) {
  if (!file || file.format !== "botpanel-options" || typeof file.pages !== "object") {
    throw new ApiError("Ce fichier est pas un fichier d'options de Bot Panel");
  }
  // Salons, catégories et rôles de CE serveur, par ID et par nom (un salon et une catégorie peuvent avoir le même nom)
  const pools = { channel: scope.channels, category: scope.categories, role: scope.roles };
  const ids = Object.fromEntries(Object.entries(pools).map(([k, list]) => [k, new Set(list.map((x) => x.id))]));
  const byName = Object.fromEntries(Object.entries(pools).map(([k, list]) => [k, new Map(list.map((x) => [x.name.toLowerCase(), x.id]))]));
  const names = file.names || {};
  const missing = new Set();
  const mapId = (raw, kind) => {
    const id = String(raw ?? "");
    if (!id || id === "0") return "0";
    if (ids[kind].has(id)) return id;
    const name = ((kind === "role" ? names.roles : names.channels) || {})[id];
    const found = name && byName[kind].get(String(name).toLowerCase());
    if (found) return found;
    missing.add(`${kind === "role" ? "@" : "#"}${name || id}`);
    return "0";
  };
  let done = 0;
  const errors = [];
  for (const p of data.pages) {
    const src = file.pages[p.id];
    if (!src || typeof src !== "object") continue;
    const values = {};
    for (const f of p.fields) {
      if (!(f.key in src)) continue;
      const v = src[f.key];
      const kind = f.type === "category" ? "category" : CHANNEL_TYPES.includes(f.type) ? "channel" : "role";
      if (["channel", "category", "role"].includes(f.type)) values[f.key] = mapId(v, kind);
      else if (["channels", "roles"].includes(f.type)) values[f.key] = (Array.isArray(v) ? v : []).map((x) => mapId(x, kind)).filter((x) => x !== "0");
      else if (f.type === "role_map") values[f.key] = Object.fromEntries(Object.entries(v || {}).map(([k, x]) => [k, mapId(x, "role")]));
      else if (f.type === "number_roles") values[f.key] = Object.fromEntries(Object.entries(v || {}).map(([n, x]) => [n, mapId(x, "role")]).filter(([, x]) => x !== "0"));
      else values[f.key] = v;
    }
    try {
      await api("POST", `/api/bots/${bot.id}/options`, { scope: scope.id, page: p.id, values });
      done += 1;
    } catch (e) {
      errors.push(`${p.title} : ${e.message}`);
    }
  }
  return { done, errors, missing: [...missing] };
}

function optionsForm(bot, data, scope, page, running) {
  const values = scope.values[page.id] || {};
  const readers = [];
  const textChannels = scope.channels.filter((c) => !c.voice);
  const NONE = "Aucun";

  function channelSelect(id, current, list) {
    const sel = h("select", { id }, h("option", { value: "0" }, NONE));
    const groups = new Map();
    for (const c of list) {
      const key = c.category || "";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    for (const [cat, items] of groups) {
      const opts = items.map((c) => h("option", { value: c.id }, `# ${c.name}`));
      sel.append(cat ? h("optgroup", { label: cat }, opts) : opts);
    }
    sel.value = current && current !== "0" ? String(current) : "0";
    if (sel.value === "" ) sel.value = "0";
    return sel;
  }
  function roleSelect(id, current) {
    const sel = h("select", { id }, h("option", { value: "0" }, NONE),
      scope.roles.map((r) => h("option", { value: r.id }, r.manageable ? r.name : `${r.name} (au-dessus du bot)`)));
    sel.value = current && String(current) !== "0" ? String(current) : "0";
    if (sel.value === "") sel.value = "0";
    return sel;
  }
  function checkList(items, selected, labelOf) {
    const set = new Set((selected || []).map(String));
    const box = h("div", { class: "checklist" }, items.map((it) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: it.id, checked: set.has(it.id) }), labelOf(it))));
    return box;
  }

  const fields = page.fields.map((f, i) => {
    const id = `opt-${i}`;
    const v = values[f.key];
    let control, read;
    switch (f.type) {
      case "bool":
        control = h("input", { type: "checkbox", id, checked: !!v });
        read = () => control.checked;
        return [h("div", { class: "field" }, h("label", { class: "check", for: id }, control,
          h("span", {}, f.label, f.help ? h("span", { class: "hint" }, f.help) : null))), () => [f.key, read()]];
      case "int":
        control = h("input", { type: "number", id, value: v ?? "", min: f.min, max: f.max });
        read = () => (control.value === "" ? undefined : Number(control.value));
        break;
      case "text":
        control = h("input", { type: "text", id, value: v ?? "" });
        read = () => control.value;
        break;
      case "textarea":
        control = h("textarea", { id, rows: 4 }); control.value = v ?? "";
        read = () => control.value;
        break;
      case "lines":
        control = h("textarea", { id, rows: 4 }); control.value = (v || []).join("\n");
        read = () => control.value.split("\n").map((x) => x.trim()).filter(Boolean);
        break;
      case "select":
        control = h("select", { id }, f.choices.map(([val, label]) => h("option", { value: val }, label)));
        control.value = v ?? (f.choices[0] || [""])[0];
        read = () => control.value;
        break;
      case "channel":
        control = channelSelect(id, v, textChannels); read = () => control.value; break;
      case "category":
        control = channelSelect(id, v, scope.categories.map((c) => ({ ...c, category: "" }))); read = () => control.value;
        [...control.querySelectorAll("option")].forEach((o) => { o.textContent = o.textContent.replace(/^# /, ""); });
        break;
      case "role":
        control = roleSelect(id, v); read = () => control.value; break;
      case "channels":
        control = checkList(textChannels, v, (c) => `# ${c.name}`);
        read = () => [...control.querySelectorAll("input:checked")].map((x) => x.value);
        break;
      case "roles":
        control = checkList(scope.roles, v, (r) => r.name);
        read = () => [...control.querySelectorAll("input:checked")].map((x) => x.value);
        break;
      case "role_map": {
        const sels = (f.keys || []).map(([k, label], j) => [k, label, roleSelect(`${id}-${j}`, (v || {})[k])]);
        control = h("div", { class: "rolemap" }, sels.map(([k, label, sel]) => h("div", {}, h("label", { for: sel.id }, label), sel)));
        read = () => Object.fromEntries(sels.map(([k, , sel]) => [k, sel.value]));
        break;
      }
      case "number_roles": {
        const rows = h("div", { class: "numroles" });
        const addRow = (n = "", role = "0") => {
          const num = h("input", { type: "number", min: f.min ?? 1, max: f.max ?? 1000, value: n, "aria-label": "Nombre" });
          const sel = roleSelect(`${id}-${rows.childElementCount}`, role);
          const row = h("div", { class: "numrow" }, num, sel, h("button", { class: "btn small danger", type: "button", onclick: () => row.remove() }, "Retirer"));
          row.read = () => [num.value, sel.value];
          rows.append(row);
        };
        Object.entries(v || {}).sort((a, b) => Number(a[0]) - Number(b[0])).forEach(([n, role]) => addRow(n, String(role)));
        control = h("div", {}, rows, h("button", { class: "btn small", type: "button", onclick: () => addRow() }, "Ajouter"));
        read = () => Object.fromEntries([...rows.children].map((r) => r.read()).filter(([n, role]) => n && role !== "0"));
        break;
      }
      default:
        return [null, null];
    }
    const labelFor = ["channels", "roles", "role_map", "number_roles"].includes(f.type) ? null : id;
    return [h("div", { class: "field" }, h("label", labelFor ? { for: labelFor } : {}, f.label), control,
      f.help ? h("span", { class: "hint" }, f.help) : null), () => [f.key, read()]];
  }).filter(([el]) => el);

  fields.forEach(([, r]) => readers.push(r));
  const save = h("button", { class: "btn primary", type: "submit", disabled: !running }, "Enregistrer");
  const form = h("form", { class: "panelbox optform" },
    h("h2", {}, page.title), page.description ? h("p", { class: "soft" }, page.description) : null,
    fields.map(([el]) => el),
    h("div", { class: "formactions" }, save, running ? null : h("span", { class: "soft small" }, "Démarre le bot pour enregistrer.")));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const out = Object.fromEntries(readers.map((r) => r()).filter(([, val]) => val !== undefined));
    act(save, async () => {
      await api("POST", `/api/bots/${bot.id}/options`, { scope: scope.id, page: page.id, values: out });
      scope.values[page.id] = { ...(scope.values[page.id] || {}), ...out };
    }, "Options enregistrées");
  });
  return form;
}

// ---- Bots partagés (comme DraftBot : on invite et on règle, sans toucher au portail Discord) ------
async function pageShared() {
  const bots = await api("GET", "/api/shared");
  const head = h("div", { class: "pagehead" }, h("div", {}, h("h1", {}, "Bots partagés"),
    h("p", { class: "soft" }, "Des bots déjà prêts : invite-les sur ton serveur et règle-les ici. Tu vois seulement les serveurs où tu as la permission « Gérer le serveur ».")));
  if (!bots.length) {
    mount(head, h("div", { class: "rack" }, h("div", { class: "empty" }, h("h2", {}, "Aucun bot partagé"),
      h("p", { class: "soft" }, ME.role === "admin" ? "Pour partager un de tes bots, coche « Bot partagé » dans ses Réglages." : "L'admin a pas encore partagé de bot."))));
    return;
  }
  mount(head, h("div", { class: "rack" }, bots.map((b) => h("div", { class: "shared" },
    h("div", { class: "shared-head" }, lamp(b.running ? "on" : "off"),
      h("div", {}, h("div", { class: "slot-name" }, b.name), h("div", { class: "slot-sub" }, b.running ? "En marche" : "Arrêté", b.yours ? " (à toi)" : ` (à ${b.owner_name})`)),
      b.invite ? h("a", { class: "btn", href: b.invite, target: "_blank", rel: "noopener" }, "Inviter sur un serveur") : null),
    b.servers.length
      ? h("ul", { class: "shared-servers" }, b.servers.map((sv) => h("li", {},
          h("span", {}, sv.name), h("a", { class: "btn small primary", href: `#/partage/${b.id}`,
            onclick: () => sessionStorage.setItem(`scope:${b.id}`, sv.id) }, "Régler"))))
      : h("p", { class: "soft small shared-none" }, "Il est sur aucun de tes serveurs. Invite-le, puis recharge cette page dans 30 secondes.")))));
}

async function pageSharedOptions(id) {
  const title = h("h1", {}, "Options");
  const body = h("div", {});
  mount(h("a", { class: "back", href: "#/partages" }, "Retour aux bots partagés"), h("div", { class: "pagehead" }, h("div", {}, title)), body);
  await tabOptions({ id }, body, (data) => { title.textContent = data.bot_name; });
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

// ---- Plugins -----------------------------------------------------------------------------------
const PLUGIN_STATES = { loaded: "on", pending: "wait", error: "bad", unknown: "off" };

async function tabPlugins(bot, body, refresh = false) {
  const data = await api("GET", `/api/bots/${bot.id}/plugins${refresh ? "?refresh=1" : ""}`);
  const reload = () => tabPlugins(bot, body);
  const restartToast = (r, what) => toast(r.restart_needed ? `${what}. Redémarre le bot pour l'appliquer.` : what);

  function variablesForm(p) {
    if (!p.variables.length) return null;
    const inputs = p.variables.map((v) => {
      const input = h("input", { type: v.secret ? "password" : "text", id: `pv-${p.id}-${v.key}`, autocomplete: "off", spellcheck: "false",
        value: v.secret ? "" : v.value, placeholder: v.secret && v.set ? "Valeur gardée (écris pour la changer)" : "" });
      input.read = () => (v.secret && v.set && !input.value ? null : input.value);
      return [v, input];
    });
    const save = h("button", { class: "btn primary", type: "submit" }, "Enregistrer");
    const form = h("form", { class: "plugvars" },
      inputs.map(([v, input]) => h("div", { class: "field" },
        h("label", { for: input.id }, v.label, v.required && !v.set ? h("span", { class: "plugneed" }, " à remplir") : null),
        input, v.help ? h("span", { class: "hint" }, v.help) : null)),
      h("div", { class: "formactions" }, save));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      act(save, async () => {
        const values = Object.fromEntries(inputs.map(([v, input]) => [v.key, input.read()]));
        restartToast(await api("PUT", `/api/bots/${bot.id}/plugins/${p.id}/variables`, { values }), "Variables enregistrées");
        reload();
      });
    });
    return form;
  }

  const installed = data.installed.map((p) => h("div", { class: "plug" },
    h("div", { class: "plug-head" }, lamp(PLUGIN_STATES[p.status] || "off"),
      h("div", {}, h("div", { class: "slot-name" }, p.name, " ", h("span", { class: "soft" }, p.version)),
        h("div", { class: "slot-sub" }, p.status_text)),
      h("div", { class: "plug-actions" },
        p.update ? h("button", { class: "btn small primary", type: "button", onclick: (e) => act(e.currentTarget, async () => {
          restartToast(await api("POST", `/api/bots/${bot.id}/plugins/install`, { id: p.id }), `${p.name} mis à jour (${p.update})`);
          reload();
        }) }, `Mettre à jour (${p.update})`) : null,
        h("button", { class: "btn small danger", type: "button", onclick: (e) => {
          if (!confirm(`Retirer le plugin « ${p.name} » de ${bot.name}? Ses réglages et ses données sont gardés.`)) return;
          act(e.currentTarget, async () => {
            restartToast(await api("DELETE", `/api/bots/${bot.id}/plugins/${p.id}`), "Plugin retiré");
            reload();
          });
        } }, "Retirer"))),
    p.description ? h("p", { class: "soft small plug-desc" }, p.description) : null,
    p.intents && p.intents.length ? h("p", { class: "small plug-desc" }, "À activer dans le Developer Portal (Bot › Privileged Gateway Intents) : ",
      h("strong", {}, p.intents.join(", "))) : null,
    variablesForm(p)));

  const have = new Set(data.installed.map((p) => p.id));
  const catalog = data.catalog.map((c) => h("div", { class: "plug" },
    h("div", { class: "plug-head" }, h("i", { class: "plug-icon", "aria-hidden": "true" }, "🧩"),
      h("div", {}, h("div", { class: "slot-name" }, c.name, " ", h("span", { class: "soft" }, c.version)),
        c.author ? h("div", { class: "slot-sub" }, `par ${c.author}`) : null),
      h("div", { class: "plug-actions" }, have.has(c.id)
        ? h("span", { class: "soft small" }, "Installé")
        : h("button", { class: "btn small primary", type: "button", onclick: (e) => act(e.currentTarget, async () => {
            restartToast(await api("POST", `/api/bots/${bot.id}/plugins/install`, { id: c.id }), `${c.name} installé`);
            reload();
          }) }, "Installer"))),
    c.description ? h("p", { class: "soft small plug-desc" }, c.description) : null));

  const fileInput = h("input", { type: "file", accept: ".zip,application/zip", id: "plug-zip" });
  const upload = h("button", { class: "btn", type: "button" }, "Téléverser le plugin");
  upload.addEventListener("click", () => act(upload, async () => {
    if (!fileInput.files.length) throw new Error("Choisis le .zip du plugin");
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    const r = await api("POST", `/api/bots/${bot.id}/plugins/upload`, fd);
    restartToast(r, `${r.name} ${r.version} installé`);
    reload();
  }));

  fill(body,
    h("div", { class: "panelbox" }, h("h2", {}, "Plugins installés"),
      h("p", { class: "soft" }, "Des modules qui ajoutent des fonctions à ton bot. Ils roulent dans le conteneur du bot, et ses réglages apparaissent dans « Options du bot ». Le bot doit supporter les plugins Bot Panel (comme ceux créés avec le modèle « Bot NG »)."),
      data.installed.length && !data.host ? h("p", { class: "plugwarn" }, "Ce bot a pas encore dit s'il supporte les plugins. Démarre-le, puis reviens ici.") : null,
      installed.length ? h("div", { class: "plugs" }, installed) : h("p", { class: "soft" }, "Aucun plugin pour l'instant.")),
    h("div", { class: "panelbox" },
      h("div", { class: "plug-title" }, h("h2", {}, "Catalogue"),
        data.catalog_enabled ? h("button", { class: "btn small", type: "button", onclick: (e) => act(e.currentTarget, () => tabPlugins(bot, body, true)) }, "Rafraîchir") : null),
      !data.catalog_enabled ? h("p", { class: "soft" }, "Le catalogue est désactivé sur ce panel (PLUGIN_CATALOG).")
        : data.catalog_error ? h("p", { class: "plugwarn" }, data.catalog_error)
        : catalog.length ? h("div", { class: "plugs" }, catalog) : h("p", { class: "soft" }, "Le catalogue est vide.")),
    h("div", { class: "panelbox" }, h("h2", {}, "Téléverser un plugin"),
      h("p", { class: "soft" }, "Un .zip qui contient plugin.json et __init__.py (voir « Plugins » dans le README de Bot Panel). Installe juste du code que tu connais : il roule avec le jeton de ton bot."),
      h("div", { class: "formactions" }, h("div", { class: "field" }, fileInput), upload)));
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
  const kitBox = bot.kit ? h("div", { class: "panelbox" }, h("h2", {}, `Modèle : ${bot.kit.name}`),
    h("p", { class: "soft" }, `Version des fichiers du modèle : ${bot.kit.version || "?"}.`,
      bot.kit.update ? ` La version ${bot.kit.update} est disponible : ça recopie les fichiers du modèle. Tes plugins, leurs réglages, les données et les variables restent.` : " C'est la dernière version."),
    bot.kit.update ? h("div", { class: "formactions" }, h("button", { class: "btn primary", type: "button", onclick: (e) => act(e.currentTarget, async () => {
      const r = await api("POST", `/api/bots/${bot.id}/kit`);
      Object.assign(bot, r); setBot(r);
      toast(r.restart_needed ? `Modèle mis à jour (${r.kit.version}). Redémarre le bot.` : `Modèle mis à jour (${r.kit.version})`);
      tabSettings(bot, body, setBot);
    }) }, `Mettre à jour (${bot.kit.update})`)) : null) : null;

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
  fill(body, general, kitBox, gitForm, danger);
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
