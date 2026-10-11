# Changelog

Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/),
versions selon [SemVer](https://semver.org/lang/fr/).

## [0.1.0] - non publiée

Première version utilisable par un agent sans intervention humaine en session
normale. Client visé : Claude Code.

### Interface pour l'agent

- Dix outils MCP : `switch_status`, `switch_connect`, `switch_disconnect`,
  `switch_screenshot`, `switch_press`, `switch_hold`, `switch_release`,
  `switch_stick`, `switch_sequence`, `switch_release_all`.
- Noms de boutons, convention des axes et durées bornées décrits dans les
  schémas des outils, et vérifiés avant d'atteindre la console.
- `switch_status` donne la prochaine étape à suivre (`next_step`), le signal
  HDMI et la dernière erreur de capture.
- Erreurs actionnables : une entrée refusée dit pourquoi et quoi faire
  (se connecter, attendre, lancer `switch-mcp doctor`, vérifier la console).
- `switch_screenshot` accepte `crop` pour agrandir une zone de l'écran.
- `screenshot=true` sur les actions renvoie l'image résultante dans le même appel.
- Prompt MCP `start_session` : état, connexion, première capture.
- Annotations MCP (lecture seule ou action) sur chaque outil.

### Manette et capture

- Émulation d'une manette Pro par NUXBT, avec reconnexion directe grâce à la
  clé d'appairage conservée par le serveur.
- Flux de rapports continu pour des appuis brefs exacts sur la Switch 2.
- Relâchement automatique de toute entrée maintenue à échéance.
- Capture HDMI ouverte à la demande, relâchée après inactivité, rouverte après
  une perte de signal et toutes les 5 minutes de capture continue (image
  décalée du pilote `sc0710`), avec contournement de son défaut de couleurs.
- Rediffusion `/preview` pour regarder pendant que l'agent joue.

### Recette et tests

- `switch-mcp selftest` : recette sur matériel réel (connexion, 20 appuis
  vérifiés par capture, déconnexion, reconnexion directe).
- Tests sans matériel des schémas, des erreurs, du relâchement quand le client
  disparaît, et de la recette contre une console simulée.

### Documentation

- README (matériel, installation, premier appairage, usage, désinstallation),
  `docs/agent.md`, `docs/depannage.md`.

[0.1.0]: https://github.com/Lucas-Rosenzweig/switch-mcp/releases/tag/v0.1.0
