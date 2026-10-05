<!--
SPDX-License-Identifier: GPL-3.0-or-later
Copyright (C) 2026 Claire Ivanenka <claire@gnu-ai.org>

This file is part of the GNU AI stack and is free software: you can
redistribute it and/or modify it under the terms of the GNU
General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.
-->

# Mistral VM (Debian GNU/Hurd) — Projet et feuille de route

`mistral-vm-debian-hurd` est le **sandbox d'intégration continue** de
la pile GNU AI : une machine Debian GNU/Hurd **headless** sous QEMU,
entièrement pilotée par ligne série, dans laquelle chaque translator
de la pile est construit et testé sur du vrai GNU/Hurd. Ce n'est pas
un translator : c'est l'infrastructure qui garantit que la pile tourne
réellement sur le système qu'elle cible, pas seulement sur Linux.

Licence : GPLv3 ou version ultérieure. Langage : Python 3.8+ côté
hôte (le driver), scripts shell côté invité.

---

## 1. Rôle et positionnement

Les images Debian GNU/Hurd préinstallées officielles
([cdimage](https://cdimage.debian.org/cdimage/ports/latest/)) sont
prévues pour une console graphique interactive. Ce dépôt les enrobe
en machine **headless**, pilotée intégralement depuis l'hôte par le
port série — utilisable par la CI de n'importe quel composant de la
pile, et réutilisable par tout projet qui doit construire, tester ou
automatiser du logiciel sur GNU/Hurd.

| Composant | Responsabilité | Lien |
|---|---|---|
| `mistral-vm-debian-hurd` | sandbox CI QEMU headless : boot, pilotage série, exécution de scripts invité | ce dépôt |
| `neuron-translator` | unité de calcul : réseau sigmoïde feedforward | gnu-ai/neuron-translator |
| `orchestrator-translator` | coordination : scheduler, supervisor, evaluator, aggregator | gnu-ai/orchestrator-translator |
| `inference-translator` | interface de dialogue : prompts, éditeur, requêtes structurées | gnu-ai/inference-translator |
| `httpfs-translator` | transport pur HTTP → système de fichiers | gnu-ai/httpfs-translator |
| `data-base-translator` | persistance PostgreSQL | gnu-ai/data-base-translator |
| `hurd` | le système lui-même ; suivi des besoins de la pile (`WIP.md`) | gnu-ai/hurd |

### Ce que ce sandbox garantit à la pile

- **Reproductibilité** : chaque exécution part d'une copie CI de
  l'image, préparée et mise en cache à l'identique ; un test qui
  passe ici repasse demain.
- **Zéro interaction humaine** : boot, login `root` sur la ligne
  série, exécution du script invité, arrêt — tout est scripté, du
  premier octet au dernier.
- **Le vrai système** : GRUB, GNU Mach et translators de l'image
  officielle, pas un simulacre ; KVM utilisé automatiquement quand
  `/dev/kvm` est disponible.

### Ce qu'il ne fait pas

- Pas de virtualisation de la pile elle-même en production : le
  palier bare-metal (cluster, datacenter) reste l'objectif — ce
  sandbox est l'étape de validation, pas la destination.

## 2. Fonctionnalités principales

- **Préparation de l'image CI** (`e2fsprogs` uniquement, pas
  d'outils GRUB) : copie de l'image téléchargée, `grub.cfg` de
  l'image patché pour `console=com0`, cache réutilisable
  (`--fresh` pour le forcer à reconstruire).
- **Driver série** (`hurd_vm.py`) : lance QEMU, pilote la console
  par socket TCP série, attend le prompt de login, se connecte en
  `root` (les images préinstallées n'ont pas de mot de passe
  root), exécute un script dans l'invité, journalise la session.
- **Options** : `--ram` (mémoire invité, défaut 1G), `--fresh`,
  script invité optionnel (sans script : boot jusqu'au prompt, puis
  arrêt).
- **Scripts invité d'exemple** : `guest-httpfs.sh` (construire et
  tester [httpfs-translator](https://github.com/gnu-ai/httpfs-translator)
  dans la VM), `guest-build-hurd.sh`, `guest-final-fix-and-build.sh`,
  `guest-restore-mach-headers.sh`.

## 3. Architecture

```
┌──────────────┐                    ┌──────────────────────────────┐
│ QEMU (KVM)   │ ── disk boot ────► │ the image's own GRUB         │
│              │                    │ with a patched grub.cfg:     │
│              │ ◄── serial line ── │ GNU Mach with console=com0  │
└──────────────┘                    └──────────────────────────────┘
       │                                        │
       └──── kernel, boot script and getty all on the serial port ──┘
```

Le driver se connecte au socket TCP série, fait le login `root` et
pilote l'invité : toute la machine est une session série.

## 4. Décisions de conception

### Le GRUB de l'image, pas un GRUB custom

Construire son propre GRUB pour Hurd est fragile et duplique le
travail de l'image officielle. Le `grub.cfg` embarqué est patché a
minima (console série) et l'image démarre sur son propre chargeur :
moins de pièces mobiles, suivi gratuit des images amont.

### Une copie CI, jamais l'original

L'image téléchargée reste vierge ; chaque run travaille sur une
copie préparée. Le cache évite de repayer la préparation, `--fresh`
la refait à la demande.

### Headless par ligne série uniquement

Pas de sortie graphique, pas de VNC : le noyau, le script de boot
et le getty passent tous sur `com0`. Ce qui n'est pas observable
sur la série n'existe pas pour le driver — et donc pour la CI.

### Login root, assumé

Les images préinstallées Debian n'ont pas de mot de passe root :
le driver s'y connecte tel quel. Le sandbox est jetable par
nature ; la sécurité est celle de l'hôte CI, pas de l'invité.

## 5. Phases

### Phase 0 — Choix de l'image et préparation CI (terminée)

Image Debian GNU/Hurd amd64 préinstallée, copie CI, patch
`grub.cfg` console série, cache.

### Phase 1 — Driver headless complet (terminée)

Boot QEMU, pilotage du socket série, login `root`, exécution d'un
script invité, arrêt propre, `--ram`/`--fresh`.

### Phase 2 — Scripts invité et intégration CI (terminée)

Scripts d'exemple (httpfs-translator, build Hurd, restauration des
en-têtes Mach), séchage des traces (`serial.log`), réutilisation
par la CI des translators de la pile.

État courant (octobre 2026) : **terminé** — le sandbox CI QEMU sert
la pile (build et `make check` des translators sur GNU/Hurd réel).
La suite ne se joue plus dans ce dépôt : passage au bare-metal
(drivers DDE, voir le `WIP.md` de
[gnu-ai/hurd](https://github.com/gnu-ai/hurd)) le jour où le
matériel cible est couvert.

## 6. Jalons synthétiques

| Phase | Contenu | Dépend de | État |
|---|---|---|---|
| 0 | Image officielle, copie CI, patch GRUB série | — | terminée |
| 1 | Driver : boot, login root, script invité, arrêt | 0 | terminée |
| 2 | Scripts d'exemple + intégration CI des translators | 1 | terminée |

Ce dépôt est une propriété permanente de la pile, pas un chantier :
tant que la pile vise GNU/Hurd, chaque translator y est construit et
testé sur le vrai système avant d'être monté en production.

Claire Ivanenka — claire@gnu-ai.org
GNU AI — https://gnu-ai.org
