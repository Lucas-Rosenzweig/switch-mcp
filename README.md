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
uv sync --extra bluetooth
```

Sans `--extra bluetooth`, seuls le faux backend et la capture sont disponibles.

## Essai sans matériel

```fish
uv run pytest
SWITCH_MCP_BACKEND=fake uv run switch-mcp
```

## Session avec la Switch

L'émulation Bluetooth a deux prérequis système.

1. **`bluetoothd` sans ses plugins**, sinon il occupe les ports HID. Pendant ce
   mode, les autres appareils Bluetooth du PC (casque, manette) ne fonctionnent
   pas. Le réglage est dans `/run` : un redémarrage du PC le retire aussi.

   ```fish
   deploy/bluetooth-session on
   ```

2. **Trois capacités réseau** pour le serveur (`CAP_NET_RAW`, `CAP_NET_ADMIN`,
   `CAP_NET_BIND_SERVICE`). Le script ci-dessous le lance sous ton utilisateur
   avec uniquement ces capacités, sans root :

   ```fish
   deploy/run-with-caps
   ```

Le serveur écoute alors sur `http://127.0.0.1:8765/mcp`. Pour le déclarer dans
Claude Code :

```fish
claude mcp add --transport http switch http://127.0.0.1:8765/mcp
```

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

Le journal du serveur est dans `~/.local/state/switch-mcp/server.log`.

Fin de session :

```fish
deploy/bluetooth-session off
```

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

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `SWITCH_MCP_BACKEND` | `nuxbt` | `nuxbt` ou `fake` |
| `SWITCH_MCP_VIDEO_DEVICE` | `/dev/video0` | Périphérique de capture |
| `SWITCH_MCP_CAPTURE_IDLE_S` | `30` | Inactivité avant de relâcher la capture |
| `SWITCH_MCP_DEFAULT_HOLD_S` | `5` | Durée d'un maintien sans durée précisée |
| `SWITCH_MCP_MAX_HOLD_S` | `30` | Durée maximale d'un maintien |
| `SWITCH_MCP_NUXBT_DEBUG` | `0` | `1` journalise chaque message échangé avec la console |
| `SWITCH_MCP_HOST` / `SWITCH_MCP_PORT` | `127.0.0.1` / `8765` | Écoute en mode HTTP |

## Limites connues

- Validé sur une Switch 2 avec un adaptateur Intel AX200 : appairage, reconnexion
  directe, appuis brefs, séquences. NUXBT ne l'annonce pas officiellement.
- Le serveur corrige trois comportements de NUXBT par surcharge à l'exécution
  (réinitialisation de l'adaptateur à l'arrêt, classe « manette » absente à la
  reconnexion, flux coupé au relâchement). Une mise à jour de NUXBT peut les
  rendre caducs : la version est bornée dans `pyproject.toml`.
- Rendre les clés au noyau remplace sa liste de clés Bluetooth classiques ;
  `deploy/bluetooth-session off` rétablit celles des autres appareils.
- Le premier flux vidéo après un changement de signal HDMI sort avec de fausses
  couleurs (défaut du pilote `sc0710`). Le serveur le contourne à l'ouverture,
  mais pas si le signal change pendant qu'une capture est en cours.
- L'agent d'appairage de NUXBT accepte toute demande d'appairage tant que le
  serveur tourne. L'adaptateur n'est visible que pendant l'appairage.
- Le serveur HTTP n'a pas d'authentification : il n'écoute que sur `127.0.0.1`.
- Le mode `stdio` convient au faux backend. Avec NUXBT, utiliser le mode HTTP :
  le serveur a besoin de capacités qu'un client MCP ne peut pas lui donner.
