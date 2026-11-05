"use strict";
const messages = {
  attente: "Ton compte est créé. L'admin doit maintenant l'approuver : reviens te connecter quand c'est fait.",
  bloque: "Ce compte est bloqué. Parle à l'admin du panel.",
  erreur: "La connexion avec Discord a pas marché. Réessaie.",
  config: "Le panel est pas encore relié à Discord : l'admin doit remplir DISCORD_CLIENT_ID et DISCORD_CLIENT_SECRET dans .env.",
};
const etat = new URLSearchParams(location.search).get("etat");
if (messages[etat]) {
  const el = document.getElementById("state");
  el.textContent = messages[etat];
  el.dataset.kind = etat === "attente" ? "wait" : "bad";
  el.hidden = false;
}
