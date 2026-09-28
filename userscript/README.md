# WikiMasters HUD (userscript Tampermonkey)

Affiche sous le prix de chaque carte de [wiki-masters.com](https://www.wiki-masters.com)
un badge **Trend Score** :

| Badge | Signification |
|---|---|
| 🟩 `▲ Trend +42 · +39 % 24 h` | tendance haussière prédite (score ≥ +5) |
| 🟥 `▼ Trend −30 · −12 % 1 j` | tendance baissière prédite (score ≤ −5) |
| ⬜ `■ Trend +2` | stable |
| 🔥 | probabilité ≥ 50 % d'un pic de trafic de +300 % sous 7 jours |
| `⏳ Suivi en cours` | carte inconnue, le tracker vient de l'ajouter (données sous ~1 min) |

Survolez un badge pour le détail (vues, probabilité de pic, version du modèle).

## Installation

1. Installez [Tampermonkey](https://www.tampermonkey.net/).
2. Ouvrez [`wikimasters-hud.user.js`](wikimasters-hud.user.js) en version *raw* : Tampermonkey propose l'installation.
3. Sur wiki-masters.com, menu Tampermonkey → **⚙️ URL de l'API…** → l'adresse de votre `api_bridge`
   (défaut `http://localhost:3000`), puis **🩺 Tester la connexion**.

> Le script appelle **votre** API (voir le README principal pour la déployer). Une page HTTPS
> ne peut appeler qu'une API en HTTPS, ou `http://localhost`. Depuis un serveur distant, passez
> par un reverse proxy HTTPS ou un tunnel SSH (`ssh -L 3000:localhost:3000 serveur`).

## Vie privée et règles du jeu

- Lecture seule : le script ne clique sur rien, n'achète et ne vend rien, et n'envoie aucune
  requête aux serveurs de wiki-masters.com.
- Seuls les **noms des cartes affichées** sont envoyés à l'API que vous avez configurée.
- Les [règles de WikiMasters](https://www.wiki-masters.com/rules) (§3) interdisent les outils
  qui jouent ou échangent à votre place ainsi que l'interception du trafic du service. Ce HUD
  ne fait ni l'un ni l'autre, mais vous restez responsable de son usage sur votre compte.

## Adapter les sélecteurs

Les sélecteurs sont regroupés dans l'objet `CONFIG.selectors`, en haut du script : carte
`[class*="aspect-[5/7]"]`, nom `h3`, et une liste de candidats pour le prix, avec un repli
sur le texte (`1 250 💰`, `980 pièces`…). Si le site change son balisage, c'est le seul endroit
à modifier. Activez **🐞 Debug** dans le menu pour tracer la détection dans la console.
