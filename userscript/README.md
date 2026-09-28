# WikiMasters HUD (userscript Tampermonkey)

Affiche sous chaque carte de [wiki-masters.com](https://www.wiki-masters.com) (enchères,
collection, page d'une enchère) un **signal de marché expliqué** :

| Badge | Signification |
|---|---|
| 🟩 `ACHAT 📈 UR dans ~2 j` | des raisons d'achat, sans prix excessif |
| 🟥 `VENDRE 📉 Perd UR dans ~4 j` | des raisons de revente |
| 🟧 `À SURVEILLER 💸 +40 % vs moy.` | signaux contradictoires (ex. bonne carte mais trop chère) |
| ⬜ `—` | rien de notable |
| `⏳ Suivi en cours` | carte inconnue, le tracker vient de l'ajouter (données sous ~1 min) |

`+2` indique d'autres raisons ; survolez le badge pour tout voir : rareté affichée vs rareté
d'après le trafic (30 j glissants et mois précédent), vues restantes avant le prochain palier,
tendance 24 h, prix vs moyenne des ventes, score ML (expérimental).

## Installation

1. Installez [Tampermonkey](https://www.tampermonkey.net/) (ou Violentmonkey).
2. Ouvrez [`wikimasters-hud.user.js`](wikimasters-hud.user.js) en version *raw* : l'installation est proposée.
3. Sur wiki-masters.com, menu Tampermonkey → **⚙️ URL de l'API…** → l'adresse de votre
   `api_bridge` (ex. `https://<machine>.<tailnet>.ts.net` avec Tailscale Serve), puis
   **🩺 Tester la connexion**.

> Une page HTTPS ne peut appeler qu'une API en HTTPS, ou `http://localhost`.

## Ce que le script lit et envoie

- **Dans la page** : les données que le jeu a déjà chargées dans ses composants React
  (`card_id`, `wikipedia_title`, rareté, enchère en cours), à défaut le texte affiché. Le
  script s'exécute pour cela dans le contexte de la page (`@sandbox JavaScript`,
  `@inject-into page`).
- **API du jeu, en lecture seule, avec votre session** (désactivable : menu **💰 Prix moyens du jeu**) :
  prix moyen des ventes par rareté et détail d'une enchère. Deux requêtes à la fois au
  maximum, 400 ms d'écart, cache de 6 h, pause automatique si le jeu répond 429.
- **Vers votre API uniquement** : une requête groupée pour les signaux, et (désactivable :
  menu **📥 Collecte des observations**) les observations — rareté affichée, prix, champs de
  la carte — pour retrouver la formule de rareté du jeu et vérifier les signaux.

Le script ne clique sur rien, ne mise pas, n'achète et ne vend rien.

## Cohabitation avec d'autres scripts

Compatible avec *WikiMasters - Affichage des prix moyens (KKH)* et *Instant Price Check* :
le badge se place après le « Moy. ventes » de KKH et affiche l'écart à la moyenne plutôt que
de la répéter.

## Adapter les sélecteurs

Tout est dans `CONFIG.selectors` en haut du script (enchères `[id^="marketplace-auction-"]`,
cartes, ligne du vendeur, badge de rareté…). Activez **🐞 Debug** pour tracer la détection
dans la console.
