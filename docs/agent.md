# Brancher un agent

Le serveur ne dépend d'aucun agent en particulier : c'est un service local qui
parle MCP en HTTP « streamable » sur `http://127.0.0.1:8765/mcp`, sans
authentification (il n'écoute que sur cette machine). `switch-mcp start` le
lance, `switch-mcp stop` l'arrête, et tout client MCP peut s'y brancher tant
qu'il tourne. Plusieurs clients peuvent être connectés en même temps ; ils
partagent alors la même manette.

`switch-mcp config --url` affiche l'adresse en vigueur si `host` ou `port` ont
été changés.

## Claude Code

`install.sh` déclare le serveur. À la main :

```fish
claude mcp add --scope user --transport http switch http://127.0.0.1:8765/mcp
```

Dans Claude Code, `/mcp` doit montrer `switch` connecté avec dix outils.

Pour démarrer, le plus simple est le prompt fourni par le serveur :
`/mcp__switch__start_session`. Il fait vérifier l'état, connecter la manette et
décrire l'écran avant toute action ; son argument facultatif `goal` donne
l'objectif de la session.

## Un autre client MCP

Sans Claude Code sur la machine, `install.sh` installe tout le reste et affiche
l'adresse à déclarer. Le client doit pouvoir afficher des images, puisque
`switch_screenshot` renvoie l'écran en JPEG.

**Client qui accepte un serveur HTTP distant** (Cursor, VS Code, Codex, Gemini
CLI, etc.). Le nom du champ varie d'un client à l'autre (`url`, `httpUrl`,
`serverUrl`) ; la forme la plus courante est :

```json
{
  "mcpServers": {
    "switch": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

**Client qui ne lance que des serveurs stdio** (Claude Desktop par son fichier
de configuration, par exemple). Un pont relaie vers le service ; il ne faut pas
lancer `switch-mcp serve` en stdio, qui n'aurait pas les droits Bluetooth :

```json
{
  "mcpServers": {
    "switch": { "command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"] }
  }
}
```

**Son propre programme.** Avec le SDK Python `mcp` :

```python
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print((await session.call_tool("switch_status", {})).content[0].text)
            await session.call_tool("switch_press", {"buttons": ["A"]})


asyncio.run(main())
```

Les consignes de jeu arrivent dans le champ `instructions` de la réponse à
`initialize` ; un client qui ne les transmet pas à son modèle gagne à les
recopier dans son prompt système. Le prompt `start_session` est disponible par
`prompts/get`.

## Sans agent du tout

Rien de ce qui suit ne passe par un modèle :

```fish
switch-mcp start      # lance le service
switch-mcp doctor     # contrôle l'installation
switch-mcp selftest   # joue la recette sur la console, comme le ferait un agent
switch-mcp stop       # rend le Bluetooth au PC
```

Pour appeler les outils à la main, l'inspecteur MCP se branche comme un client
(transport « Streamable HTTP », adresse ci-dessus) :

```fish
npx @modelcontextprotocol/inspector
```

## Ce que l'agent sait sans qu'on le lui dise

Le serveur envoie à l'agent des consignes générales, et chaque outil décrit ses
paramètres, ses bornes et ses exemples dans son schéma. Un agent qui ne connaît
pas le projet sait donc :

- commencer par `switch_status` et suivre son champ `next_step` ;
- se connecter avec `switch_connect`, puis interroger `switch_status` jusqu'à
  `connected` (les états intermédiaires sont `connecting` ou `reconnecting`,
  puis `settling` pendant environ 3 s) ;
- les noms de boutons : `A B X Y L R ZL ZR PLUS MINUS HOME CAPTURE`, la croix
  `UP DOWN LEFT RIGHT`, les clics de stick `L_STICK R_STICK` ;
- la convention des sticks : `x` et `y` entre -1 et 1, `+x` à droite, `+y` en haut ;
- les durées bornées : appui de 20 ms à 5 s, maintien jusqu'à 30 s, séquence
  de 64 étapes et 60 s au plus.

Les erreurs disent quoi faire plutôt que de renvoyer une trace, par exemple :

> The controller is idle, so inputs are refused. Call switch_connect.

> No picture: … Ask the user to check that the Switch is docked and awake …

Quand un message dit que seul l'humain peut agir (console en veille, carte de
capture prise par un autre programme, menu d'appairage à ouvrir), l'agent
doit s'arrêter et le dire.

## Bonnes pratiques de jeu

**Regarder, agir brièvement, regarder.** Une action n'est jamais supposée
réussie : l'agent vérifie l'image suivante. `screenshot=true` sur
`switch_press`, `switch_stick` ou `switch_sequence` renvoie l'image dans le même
appel, ce qui économise un aller-retour. `settle_ms` règle l'attente avant
l'image (300 ms par défaut ; plus pour une transition d'écran).

**Menus.** Un appui de 100 ms déplace le curseur d'exactement une case. Pour
plusieurs cases, une séquence avec des pauses est plus fiable que des appuis
successifs :

```json
[{"ms": 100, "buttons": ["DOWN"]}, {"ms": 150}, {"ms": 100, "buttons": ["DOWN"]}]
```

**A et B.** Disposition Nintendo : **A** valide (bouton de droite), **B**
revient en arrière (bouton du bas).

**Déplacements.** `switch_stick` incline un stick sans bloquer, le temps de
regarder et d'ajuster. Pour une distance précise, `switch_sequence` avec une
durée fixe est reproductible ; exemple, courir à droite puis sauter en courant :

```json
[{"ms": 400, "left": [1, 0]}, {"ms": 150, "left": [1, 0], "buttons": ["B"]}]
```

**Entrées combinées.** `switch_hold` maintient un bouton (accélérer, viser)
pendant que d'autres appels appuient ou orientent. Tout maintien se relâche
seul à échéance (5 s par défaut) : l'agent le renouvelle en le rappelant.
Si l'agent est interrompu, rien ne reste enfoncé sur la console.

**Lire un détail.** Plutôt qu'une image `detail` entière, `switch_screenshot`
avec `crop` agrandit une zone : `[x, y, largeur, hauteur]` en fractions de
l'image, par exemple `[0.75, 0, 0.25, 0.2]` pour le coin haut droit.

**Coût.** Une image `normal` (1280 px) suffit presque toujours ; `fast`
(960 px) pour suivre une action rapide en économisant du contexte.

## Regarder en même temps

```fish
ffplay -fflags nobuffer http://127.0.0.1:8765/preview
```

Ne pas ouvrir la carte de capture directement (OBS, `ffplay /dev/video0`) :
l'agent perdrait l'image.
