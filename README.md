# switch-mcp

Serveur MCP qui permet à un agent IA de jouer sur une Nintendo Switch : il voit
l'écran par une carte de capture HDMI et agit par une manette Pro émulée en
Bluetooth.

## Architecture

```
agent IA ──MCP──> switch_mcp.server
                    ├─ Pad            état de manette persistant, délais, relâchement automatique
                    │   └─ Backend    nuxbt (Bluetooth réel) ou fake (tests)
                    └─ Capture        /dev/video0 à la demande
```

- `controller/base.py` : l'état de manette et l'interface que chaque backend implémente.
- `controller/pad.py` : entrées simultanées, maintien avec échéance, séquences minutées.
- `controller/nuxbt_backend.py` : émulation Bluetooth via [NUXBT](https://github.com/hannahbee91/nuxbt).
- `capture.py` : capture ouverte à la première image, relâchée après 30 s d'inactivité, rouverte après une perte de signal.

## Installation

```fish
./install.sh
```

Le script, à relancer après chaque mise à jour, demande le mot de passe `sudo`
une seule fois. Il installe l'environnement Python (`uv sync --extra bluetooth`),
la commande `switch-mcp` dans `~/.local/bin`, le service systemd
`switch-mcp.service` (non lancé au démarrage du PC), une règle polkit qui permet
de le démarrer et l'arrêter sans mot de passe, un fichier de configuration
`~/.config/switch-mcp/config.toml` et le serveur MCP `switch` dans Claude Code
(niveau utilisateur). `./install.sh --uninstall` retire tout cela, sauf la
configuration et la clé d'appairage.

## Essai sans matériel

```fish
uv sync
uv run pytest
SWITCH_MCP_BACKEND=fake uv run switch-mcp serve
```

## Session avec la Switch

```fish
switch-mcp start     # bascule le Bluetooth en mode émulation et lance le serveur
switch-mcp stop      # arrête le serveur et rétablit le Bluetooth normal
switch-mcp status    # état du service, mode Bluetooth, adresse MCP
switch-mcp doctor    # vérifie adaptateur, BlueZ, service, carte de capture, signal HDMI, clé, Claude Code
```

Pendant une session, `bluetoothd` tourne sans ses plugins pour libérer les ports
HID : les autres appareils Bluetooth du PC (casque, manette) ne fonctionnent pas.
Le serveur tourne sous ton utilisateur avec seulement trois capacités réseau
(`CAP_NET_ADMIN`, `CAP_NET_RAW`, et `CAP_NET_BIND_SERVICE` pour les ports L2CAP
HID 17 et 19). Le basculement du Bluetooth est fait, en root, par
`/usr/local/libexec/switch-mcp/bluetooth-mode`, copie non modifiable par
l'utilisateur de `deploy/bluetooth-mode`. `switch-mcp bt on|off` le fait à la
main en cas de besoin.

Le serveur écoute alors sur `http://127.0.0.1:8765/mcp`, déjà déclaré dans Claude
Code par `install.sh`. En développement, `touch .reload` relance le serveur avec
le code courant sans repasser par le Bluetooth normal.

Premier appairage : sur la Switch, ouvrir *Manettes > Changer le style/l'ordre*,
puis appeler `switch_connect` et interroger `switch_status` jusqu'à `connected`.
La console a trois minutes pour trouver la manette ; rappeler `switch_connect`
relance l'appairage.

Les fois suivantes, `switch_connect` se reconnecte directement en deux secondes,
console allumée et **hors** de l'écran *Changer le style/l'ordre*.

### Pourquoi le serveur conserve la clé Bluetooth

La Switch 2 appaire en « No Bonding » : Linux oublie alors la clé de liaison à la
déconnexion, tandis que la console garde la sienne et refuse ensuite tout nouvel
appairage. Le serveur enregistre donc la clé dans
`~/.local/state/switch-mcp/link_keys.json` (lisible par toi seul) et la rend au
noyau avant chaque reconnexion, comme le ferait une vraie manette.

Si ce fichier est perdu, la console refusera l'appairage : il faut alors faire
*Paramètres de la console > Manettes et accessoires > Déconnecter les manettes*
sur la Switch, puis appairer à nouveau.

Le journal du serveur est dans `~/.local/state/switch-mcp/server.log`, et aussi
dans `journalctl -u switch-mcp`.

## Regarder l'écran pendant que l'agent joue

La carte de capture n'accepte qu'un seul lecteur : si `ffplay` ou OBS l'ouvre,
l'agent ne voit plus rien. Le serveur rediffuse donc son propre flux :

```fish
ffplay -fflags nobuffer http://127.0.0.1:8765/preview
```

L'adresse s'ouvre aussi directement dans un navigateur.

## Outils MCP

| Outil | Rôle |
| --- | --- |
| `switch_status` | État de la connexion, entrées maintenues, capture ouverte ou non |
| `switch_connect` / `switch_disconnect` | Appairage ou reconnexion sans bloquer, et déconnexion |
| `switch_screenshot` | Image actuelle (`fast`, `normal`, `detail`) |
| `switch_press` | Appui bref sur un ou plusieurs boutons, sans toucher aux entrées maintenues |
| `switch_hold` / `switch_release` | Maintien non bloquant, avec échéance |
| `switch_stick` | Inclinaison non bloquante d'un stick, avec échéance |
| `switch_sequence` | Suite d'états minutés en un seul appel |
| `switch_release_all` | Tout relâcher |

`switch_press`, `switch_stick` et `switch_sequence` acceptent `screenshot=true`
pour renvoyer l'image résultante dans le même appel.

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
| `screenshot_width` | `1280` | Largeur d'une capture par défaut |
| `default_hold_s` | `5` | Durée d'un maintien sans durée précisée |
| `max_hold_s` | `30` | Durée maximale d'un maintien |
| `settle_s` | `3` | Délai après connexion avant d'accepter les entrées |
| `switch_address` | (vide) | Console à reconnecter ; apprise à l'appairage |
| `host` / `port` | `127.0.0.1` / `8765` | Écoute du serveur MCP |

`SWITCH_MCP_NUXBT_DEBUG=1` journalise chaque message échangé avec la console.

## Limites connues

- Validé sur une Switch 2 avec un adaptateur Intel AX200 : appairage, reconnexion
  directe, appuis brefs, séquences. NUXBT ne l'annonce pas officiellement.
- Le serveur corrige trois comportements de NUXBT par surcharge à l'exécution
  (réinitialisation de l'adaptateur à l'arrêt, classe « manette » absente à la
  reconnexion, flux coupé au relâchement). Une mise à jour de NUXBT peut les
  rendre caducs : la version est bornée dans `pyproject.toml`.
- Rendre les clés au noyau remplace sa liste de clés Bluetooth classiques ;
  `switch-mcp stop` rétablit celles des autres appareils.
- Le premier flux vidéo après un changement de signal HDMI sort avec de fausses
  couleurs (défaut du pilote `sc0710`). Le serveur le contourne à l'ouverture,
  mais pas si le signal change pendant qu'une capture est en cours.
- L'agent d'appairage de NUXBT accepte toute demande d'appairage tant que le
  serveur tourne. L'adaptateur n'est visible que pendant l'appairage.
- Le serveur HTTP n'a pas d'authentification : il n'écoute que sur `127.0.0.1`.
- Le mode `stdio` convient au faux backend. Avec NUXBT, utiliser le mode HTTP :
  le serveur a besoin de capacités qu'un client MCP ne peut pas lui donner.
