# switch-mcp

Serveur MCP qui permet à un agent IA de jouer sur une Nintendo Switch : il voit
l'écran par une carte de capture HDMI et agit par une manette Pro émulée en
Bluetooth. Une fois installé, l'agent se branche sur une seule adresse,
`http://127.0.0.1:8765/mcp`, et n'a besoin de personne pour jouer.

- [Brancher un agent et bien jouer](docs/agent.md)
- [Dépannage](docs/depannage.md)
- [Historique des versions](CHANGELOG.md)

## Matériel requis

| Élément | Testé avec | Remarque |
| --- | --- | --- |
| PC Linux avec systemd et BlueZ | CachyOS | Python ≥ 3.12 et [`uv`](https://docs.astral.sh/uv/) |
| Adaptateur Bluetooth | Intel AX200 intégré | Il sert à la manette pendant une session : casque et autres manettes du PC sont coupés tant que le serveur tourne |
| Carte de capture HDMI | Elgato à pilote `sc0710` | Vue comme `/dev/video0` ; pilote hors noyau, à recompiler après une mise à jour du noyau |
| Console | Switch 2, dockée | La Switch 1 devrait fonctionner, non vérifié |

La sortie HDMI du dock va dans la carte de capture. Pour voir aussi l'écran sur
une télé, il faut un répartiteur HDMI ; sinon regarde la vue en direct du
serveur (voir plus bas).

## Installation

```fish
git clone git@github.com:Lucas-Rosenzweig/switch-mcp.git
cd switch-mcp
./install.sh
```

Le script, à relancer après chaque mise à jour, demande le mot de passe `sudo`
une seule fois. Il installe l'environnement Python (`uv sync --extra bluetooth`),
la commande `switch-mcp` dans `~/.local/bin`, le service systemd
`switch-mcp.service` (non lancé au démarrage du PC), une règle polkit qui permet
de le démarrer et l'arrêter sans mot de passe, un fichier de configuration
`~/.config/switch-mcp/config.toml` et le serveur MCP `switch` dans Claude Code
(niveau utilisateur).

Vérifie ensuite que tout est en place :

```fish
switch-mcp doctor
```

`doctor` contrôle l'adaptateur Bluetooth, BlueZ, les capacités du service, la
carte de capture, le signal HDMI et la clé d'appairage enregistrée, et dit quoi
faire pour chaque point qui ne va pas.

## Premier appairage

À faire une seule fois, devant la console.

1. Démarre le serveur : `switch-mcp start`.
2. Sur la Switch, ouvre **Manettes** depuis le menu HOME, puis **Changer le
   style/l'ordre**. La console cherche alors de nouvelles manettes.
3. Demande à l'agent de se connecter (ou appelle l'outil `switch_connect`). La
   console a trois minutes pour trouver la manette ; elle apparaît comme une
   manette Pro.
4. Quand `switch_status` indique `connected`, quitte l'écran *Changer le
   style/l'ordre* avec **A**.

Le serveur garde la clé d'appairage dans `~/.local/state/switch-mcp/link_keys.json`
(lisible par toi seul). Les fois suivantes, il se reconnecte directement en
quelques secondes, sans passer par ce menu.

## Utilisation au quotidien

```fish
switch-mcp start     # bascule le Bluetooth en mode émulation et lance le serveur
switch-mcp stop      # arrête le serveur et rétablit le Bluetooth normal
switch-mcp status    # état du service, mode Bluetooth, adresse MCP
```

Console allumée et dockée, il suffit ensuite de demander à Claude Code de jouer.
L'agent lit l'état, se reconnecte, regarde l'écran et agit ; le détail est dans
[docs/agent.md](docs/agent.md).

Pendant une session, `bluetoothd` tourne sans ses plugins pour libérer les ports
HID : les autres appareils Bluetooth du PC (casque, manette) ne fonctionnent pas.
`switch-mcp stop` les rend. Le serveur tourne sous ton utilisateur avec
seulement trois capacités réseau (`CAP_NET_ADMIN`, `CAP_NET_RAW`, et
`CAP_NET_BIND_SERVICE` pour les ports L2CAP HID 17 et 19) ; le basculement du
Bluetooth est fait en root par `/usr/local/libexec/switch-mcp/bluetooth-mode`,
copie non modifiable de `deploy/bluetooth-mode`. `switch-mcp bt on|off` le fait
à la main en cas de besoin.

### Regarder l'écran pendant que l'agent joue

La carte de capture n'accepte qu'un seul lecteur : si OBS ou `ffplay` l'ouvre,
l'agent ne voit plus rien. Le serveur rediffuse donc son propre flux :

```fish
ffplay -fflags nobuffer http://127.0.0.1:8765/preview
```

L'adresse s'ouvre aussi dans un navigateur.

### Vérifier que tout marche : la recette

```fish
switch-mcp selftest
```

Serveur démarré, la recette se connecte comme un agent, puis te demande
d'ouvrir **Paramètres > Manettes et accessoires > Tester les périphériques
d'entrée > Tester les boutons**. Elle fait alors 20 appuis brefs et vérifie
sur la capture que chacun ajoute son icône à l'écran (une touche restée
enfoncée ne pourrait pas en ajouter une nouvelle), se déconnecte, et contrôle
la reconnexion directe. Ne maintiens aucune touche pendant ce temps : un
maintien met fin à l'écran de test. Elle finit par « Recette réussie » ou par la
liste de ce qui a échoué.

## Outils MCP

| Outil | Rôle |
| --- | --- |
| `switch_status` | État de la connexion, prochaine étape conseillée, entrées maintenues, signal HDMI, dernière erreur |
| `switch_connect` / `switch_disconnect` | Reconnexion directe ou appairage, sans bloquer ; déconnexion |
| `switch_screenshot` | Image fraîche (`fast`, `normal`, `detail`), avec recadrage pour lire un détail |
| `switch_press` | Appui bref sur un ou plusieurs boutons, sans toucher aux entrées maintenues |
| `switch_hold` / `switch_release` | Maintien non bloquant, relâché automatiquement à échéance |
| `switch_stick` | Inclinaison non bloquante d'un stick, avec échéance |
| `switch_sequence` | Suite d'états minutés en un seul appel |
| `switch_release_all` | Tout relâcher |

`switch_press`, `switch_stick` et `switch_sequence` acceptent `screenshot=true`
pour renvoyer l'image résultante dans le même appel. Le serveur propose aussi
le prompt `start_session`, qui enchaîne état, connexion et première capture.

## Configuration

`~/.config/switch-mcp/config.toml` (modèle commenté : `deploy/config.example.toml`).
Chaque réglage peut être surchargé par une variable d'environnement
`SWITCH_MCP_<NOM>` ; `SWITCH_MCP_CONFIG` désigne un autre fichier.
`switch-mcp config` affiche les valeurs en vigueur.

| Réglage | Défaut | Rôle |
| --- | --- | --- |
| `backend` | `nuxbt` | `nuxbt` ou `fake` |
| `video_device` | `/dev/video0` | Périphérique de capture |
| `capture_idle_s` | `30` | Inactivité avant de relâcher la capture |
| `screenshot_width` | `1280` | Largeur des captures `normal`, celles par défaut de `switch_screenshot` |
| `default_hold_s` | `5` | Durée d'un maintien sans durée précisée |
| `max_hold_s` | `30` | Durée maximale d'un maintien |
| `settle_s` | `3` | Délai après connexion avant d'accepter les entrées |
| `switch_address` | (vide) | Console à reconnecter ; apprise à l'appairage |
| `host` / `port` | `127.0.0.1` / `8765` | Écoute du serveur MCP |

`SWITCH_MCP_NUXBT_DEBUG=1` journalise chaque message échangé avec la console.

## Architecture

```
agent IA ──MCP HTTP──> switch_mcp.server
                         ├─ Pad            état de manette persistant, échéances, relâchement automatique
                         │   └─ Backend    nuxbt (Bluetooth réel) ou fake (tests)
                         └─ Capture        /dev/video0 ouvert à la demande
```

- `controller/base.py` : l'état de manette et l'interface que chaque backend implémente.
- `controller/pad.py` : entrées simultanées, maintien avec échéance, séquences minutées.
- `controller/nuxbt_backend.py` : émulation Bluetooth via [NUXBT](https://github.com/hannahbee91/nuxbt).
- `capture.py` : capture ouverte à la première image, relâchée après 30 s d'inactivité, rouverte après une perte de signal.
- `server.py` : les outils MCP, leurs schémas et les messages d'erreur destinés à l'agent.
- `selftest.py` : la recette sur matériel réel.

Le transport est HTTP local et non stdio : la liaison Bluetooth doit survivre
aux sessions de l'agent, et le service a besoin de capacités réseau que
l'agent n'a pas.

### Pourquoi le serveur conserve la clé Bluetooth

La Switch 2 appaire en « No Bonding » : Linux oublie alors la clé de liaison à
la déconnexion, tandis que la console garde la sienne et refuse ensuite tout
nouvel appairage. Le serveur enregistre donc la clé et la rend au noyau avant
chaque reconnexion, comme le ferait une vraie manette.

## Développement

```fish
uv sync --extra bluetooth   # sans --extra bluetooth : faux backend et capture seulement
uv run pytest               # aucun matériel requis
SWITCH_MCP_BACKEND=fake uv run switch-mcp serve
```

En développement, `touch .reload` relance le serveur installé avec le code
courant. Le journal du serveur est dans `~/.local/state/switch-mcp/server.log`,
et aussi dans `journalctl -u switch-mcp`.

## Désinstallation

```fish
switch-mcp stop
./install.sh --uninstall
```

Cela retire le service, la règle polkit, la commande et la déclaration dans
Claude Code. La configuration (`~/.config/switch-mcp/`) et la clé d'appairage
(`~/.local/state/switch-mcp/`) restent ; supprime ces dossiers pour les effacer, puis sur la Switch fais **Paramètres > Manettes et accessoires
> Déconnecter les manettes** pour que la console oublie aussi la sienne.

## Limites connues

- Validé sur une Switch 2 avec un Intel AX200 : appairage, reconnexion directe,
  appuis brefs, séquences. NUXBT ne l'annonce pas officiellement.
- La Switch 2 oublie parfois la manette après plusieurs reconnexions ; il faut
  alors refaire le premier appairage (voir [docs/depannage.md](docs/depannage.md)).
- Le serveur corrige trois comportements de NUXBT à l'exécution ; une mise à
  jour de NUXBT peut les rendre caducs, d'où la version bornée dans `pyproject.toml`.
- Le premier flux vidéo après un changement de signal HDMI sort avec de fausses
  couleurs (pilote `sc0710`). Le serveur le contourne à l'ouverture, pas si le
  signal change pendant une capture.
- L'agent d'appairage de NUXBT accepte toute demande tant que le serveur tourne ;
  l'adaptateur n'est visible que pendant l'appairage.
- Rendre les clés au noyau remplace sa liste de clés Bluetooth classiques ;
  `switch-mcp stop` rétablit celles des autres appareils.
- Le serveur HTTP n'a pas d'authentification : il n'écoute que sur `127.0.0.1`.
- Le mode `stdio` convient au faux backend. Avec NUXBT, utiliser le mode HTTP :
  le serveur a besoin de capacités qu'un client MCP ne peut pas lui donner.
