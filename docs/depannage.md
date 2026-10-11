# Dépannage

Commence toujours par :

```fish
switch-mcp doctor
```

Il vérifie l'adaptateur, BlueZ, les capacités du service, la carte de capture,
le signal HDMI et la clé d'appairage, et dit quoi faire. Le journal du serveur
est dans `~/.local/state/switch-mcp/server.log`.

## La Switch a oublié la manette

**Signe.** `switch_status` reste sur `connecting` ou `reconnecting` alors que
la console est allumée et hors du menu d'appairage ; la recette échoue à
« Reconnexion directe ». Sur la console, la manette n'apparaît plus dans
*Manettes*.

**Cause.** La Switch 2 efface parfois l'appairage après quelques
reconnexions ; la cause est encore à l'étude. La clé gardée par le serveur ne
correspond alors plus à rien.

**Remède.**

1. Sur la Switch, ouvre **Manettes > Changer le style/l'ordre**.
2. Appelle `switch_connect` (ou demande à l'agent de se connecter) et attends
   `connected`.
3. Quitte le menu avec **A**.

Si la console refuse l'appairage, la clé côté console est probablement
périmée : fais **Paramètres > Manettes et accessoires > Déconnecter les
manettes** sur la Switch, puis recommence.

Ne supprime pas `~/.local/state/switch-mcp/link_keys.json` sans raison : sans
lui, la console refuse le nouvel appairage tant qu'on n'a pas déconnecté les
manettes de son côté.

## Pas d'image : carte de capture occupée

**Signe.** `switch_screenshot` répond « Cannot open /dev/video0; is another
program using the capture card? ».

**Cause.** La carte n'accepte qu'un lecteur à la fois : OBS, `ffplay
/dev/video0`, un navigateur ou une ancienne instance du serveur la tient.

**Remède.** Ferme ce programme. Pour trouver lequel :

```fish
fuser -v /dev/video0
```

Pour regarder l'écran pendant que l'agent joue, utilise la rediffusion du
serveur : `ffplay -fflags nobuffer http://127.0.0.1:8765/preview`.

## Pas d'image : pas de signal HDMI

**Signe.** « No frame from the capture card (is the Switch docked and on?) »,
ou `hdmi_signal: false` dans `switch_status`.

**Remède.** Vérifie que la Switch est dans son dock, allumée et pas en veille,
et que la sortie HDMI du dock arrive bien sur la carte. Après un changement de
signal, le serveur rouvre la carte tout seul en une ou deux secondes.

## Couleurs fausses (image sarcelle, texte rose)

**Cause.** Défaut du pilote `sc0710` : le premier flux ouvert après un
changement de signal HDMI (console dockée, sortie de veille) arrive non
converti.

**Remède.** Le serveur ouvre un flux jetable avant le vrai, ce qui suffit à
l'ouverture. Si les couleurs se faussent pendant une capture déjà ouverte,
laisse l'agent sans capture 30 s (la carte est alors relâchée) ou redémarre le
serveur : `switch-mcp stop && switch-mcp start`.

## Image décalée ou enroulée

**Signe.** L'image est coupée et décalée, une partie du bas apparaît en haut
(ou un côté passe de l'autre), et cela persiste d'une capture à l'autre. Vu
une fois après 11 minutes de captures continues.

**Cause.** Le flux du pilote `sc0710` perd son alignement ; il ne le retrouve
qu'à la réouverture de la carte.

**Remède.** Le serveur rouvre la carte toutes les 5 minutes de capture
continue, ce qui borne la durée du défaut. Pour le corriger tout de suite,
laisse l'agent sans capture 30 s (la carte est relâchée) ou redémarre le
serveur : `switch-mcp stop && switch-mcp start`.

## La carte de capture a disparu après une mise à jour du noyau

**Signe.** `/dev/video0` n'existe plus ; `doctor` signale la carte absente.

**Cause.** Le pilote `sc0710` n'est pas dans le noyau : il est compilé pour
une version précise et doit l'être à nouveau après chaque mise à jour.

**Remède.** S'il a été installé avec DKMS :

```fish
sudo dkms autoinstall
sudo modprobe sc0710
```

Sinon, recompile-le depuis ses sources comme lors de son installation, puis
`sudo modprobe sc0710`. Installe d'abord les en-têtes du nouveau noyau
(paquet `linux-cachyos-headers` ou l'équivalent du noyau utilisé).

## Casque et manettes Bluetooth du PC coupés

**Cause.** Pendant une session, BlueZ tourne sans ses plugins pour libérer les
ports HID de la manette émulée. Les autres appareils Bluetooth du PC ne
fonctionnent pas dans ce mode.

**Remède.** `switch-mcp stop` rétablit le Bluetooth normal. Un redémarrage du
PC le rétablit aussi.

## « The PC is not ready for Bluetooth emulation »

**Cause.** Le serveur a démarré sans les capacités réseau ou avec BlueZ en
mode normal, typiquement lancé à la main au lieu de `switch-mcp start`.

**Remède.** `switch-mcp stop`, puis `switch-mcp start`, puis `switch-mcp
doctor` si le message persiste.

## Une touche semble rester enfoncée

Cela ne devrait pas arriver : tout maintien a une échéance et se relâche seul,
même si l'agent est interrompu. En cas de doute, `switch_release_all`, ou
`switch_disconnect` puis `switch_connect`.
