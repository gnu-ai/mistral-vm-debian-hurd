<!--
SPDX-License-Identifier: GPL-3.0-or-later
Copyright (C) 2026 Claire Ivanenka <claire@gnu-ai.org>

Feuille de route consolidée de la pile GNU AI, dérivée des PLAN.md
de chaque dépôt. Ordre d'écriture des translators dicté par les
dépendances croisées (section « Jalons » de chaque PLAN.md).
-->

# GNU AI — ToDo consolidé (pile complète)

## 1. État des dépôts (octobre 2026)

| Dépôt | État | Phase courante |
|---|---|---|
| `neuron-translator` | Terminé (v1.0, src + tests) | — |
| `mistral-vm-debian-hurd` | Terminé (sandbox CI QEMU) | — |
| `httpfs-translator` | En cours : phases 2–3 faites (transport + htmlfs/jsonfs/csvfs) | 1 (CI) + fin 3 (cluster) + 4 |
| `data-base-translator` | Pas de code (PLAN.md seulement) | **0 à coder en premier** |
| `orchestrator-translator` | Pas de code (PLAN.md seulement) | **0, juste après data-base** |
| `inference-translator` | Pas de code (PLAN.md seulement) | 0–1, fil parallèle |

## 2. Ordre de priorité — translators à coder en premier

Justification par les dépendances des PLAN.md :

1. **`data-base-translator` phases 0–1** — l'orchestrateur intègre PostgreSQL
   *dès sa phase 1* et écrit directement contre le contrat gelé : rien ne peut
   avancer sans le périmètre minimal (création du schéma + écriture de `runs`
   et `run_instances`).
2. **`orchestrator-translator` phases 0–1** — cœur de la pile ; sa phase 0
   (contrats + schéma SQL) est co-gelée avec data-base phase 0 (revue croisée).
3. **`inference-translator` phases 0–1** — prérequis direct de la phase 3 de
   l'orchestrateur (acquisition réseau) ; peut avancer en parallèle de la
   phase 1 de l'orchestrateur.
4. **`httpfs-translator`** (fin de phase 1 CI + item cluster de phase 3) —
   l'essentiel est déjà servi ; finitions à intercaler.
5. Le reste suit l'ordre des phases ci-dessous.

## 3. P0 — Critique (bloque toute la pile)

### data-base-translator
- [ ] Phase 0 : reprise du contrat `orchestrator → database` + registre de
      clés SSH (`inference → database`) ; gel de l'arborescence `/db` ;
      `schema.sql` (source de vérité, section 6 du PLAN) ; conventions de
      montage (`conninfo`, options `create`/`verify`/`readonly`).
- [ ] Phase 0 : livrable `SPEC.md` + squelette compilable ; **revue croisée**
      avec orchestrator-translator et inference-translator.

### orchestrator-translator
- [ ] Phase 0 : gel des contrats (section 3.3), format du descripteur de
      tâche JSON, couche `storage_write`/`storage_read` (backend
      data-base-translator par défaut, mémoire pour tests uniquement),
      points de montage `/llm<N>`, `/web`, `/inference`, `/db`,
      `/orchestrate`, trio JSON `request`/`status`/`result`.
- [ ] Phase 0 : livrable `SPEC.md` + squelette compilable.

## 4. P1 — MVP de la pile (data-base + orchestrator, synchronisés)

### data-base-translator
- [ ] Phase 1 : connexion libpq au montage, création idempotente du schéma,
      `/db/status` lisible.
- [ ] Phase 1 : écritures préparées de `runs`, `run_instances`,
      `training_data` avec retour d'identifiant ; doublon `checksum` rendu
      comme statut `duplicate`.
- [ ] Phase 1 : compilable sous Hurd, `make check` vert.
- [ ] Acceptation : la phase 1 de l'orchestrateur persiste ses
      `runs`/`run_instances` via `/db`, lignes visibles côté SQL.

### orchestrator-translator
- [ ] Phase 1 : scheduler (N instances `neuron-translator` sur `/llm1…N`,
      topologies différentes), distribution des entrées, collecte des sorties.
- [ ] Phase 1 : aggregator (vote majoritaire + moyenne uniforme) ;
      translator `/orchestrate` minimal : `run`, `status`, `result` (trio
      JSON types 2–3).
- [ ] Phase 1 : persistance dès la première exécution via `/db`.
- [ ] Acceptation : exécution complète sous Debian GNU/Hurd (QEMU), 2 puis 8
      instances, `make check` vert, trace dans `runs`/`run_instances`.

### httpfs-translator (à intercaler, sans dépendance)
- [ ] Phase 1 : workflow GitHub Actions Debian GNU/Hurd sous QEMU
      (via mistral-vm-debian-hurd).
- [ ] Phase 1 : suite de tests C embarquée `libmicrohttpd` v1 (Range, réponses
      tronquées, codes d'erreur, SHA256 post-lecture) si non couverte
      intégralement par les tests existants.

## 5. P2 — Fils de travail parallèles (après le MVP)

Deux fils avancent en parallèle : **1→2** (supervisor) et **1→3**
(acquisition réseau).

### Fil A : supervisor (orchestrator 2 + data-base 2)
- [ ] inference-translator phase 0 : gel du trio JSON, arborescence
      `/inference`, mode distant (SSH, trames, clés), protocole éditeur ;
      `SPEC.md` + squelette compilable.
- [ ] inference-translator phase 1 : translator `/inference` monté, `write`
      du prompt, extraction d'URLs, `read` de `request`/`urls`/`task`/
      `status`, détection heuristique de tâche ; `make check` vert.
- [ ] orchestrator-translator phase 2 : supervisor (timeout, redémarrage,
      journal des défaillances), parallélisme, file d'attente, limites de
      ressources ; chaque incident persisté dans `incidents`.
- [ ] data-base-translator phase 2 : écriture de `incidents`, requêtes de
      lecture (`where`, `order`), navigation `/db/runs/<id>` et
      `/db/runs/<id>/instances`.
- [ ] Acceptation fil A : une tâche survit à la perte d'une instance ; le
      run, ses instances et l'incident sont relus intégralement depuis `/db`.

### Fil B : acquisition réseau (orchestrator 3 + data-base 3)
- [ ] orchestrator-translator phase 3 : lecture de `/inference/request`,
      montage dynamique de `httpfs`, politique non-200 documentée,
      transformation contenu → vecteurs, archivage dans `training_data`
      avant usage.
- [ ] data-base-translator phase 3 : contenus paginés, curseur POSIX,
      empreinte vérifiée, requêtes par date/statut/absence en base
      (anti-doublon avant téléchargement).
- [ ] httpfs-translator fin de phase 3 : validation de l'opération
      multi-nœuds du cluster (un curseur seek/Range par fichier ouvert et
      par lecteur, stacking sans topologie mono-nœud).
- [ ] Acceptation fil B : tâche complète « prompt → URL → contenu → N réseaux
      → résultat » ; démonstration avec une URL réelle et une 404, les deux
      traces dans `training_data`, relues sans réseau.

## 6. P3 — Rejouabilité et interface (après fils A et B)

- [ ] data-base-translator phase 4 : export de flux rejouables, statistiques
      par topologie en SQL, pagination bornée.
- [ ] orchestrator-translator phase 4 : commandes `replay` et `history`,
      rejeu depuis les données archivées (réseau coupé), historique des
      `run_instances`.
- [ ] inference-translator phase 2 : client `inference` (bannière, saisie
      multi-lignes termios, `$VISUAL`/`$EDITOR` → nano → vi, commandes
      `/editor`, `/task`, `/submit`, `/status`, `/quit`, narration).
- [ ] inference-translator phase 3 : couleurs ANSI 256, coloration
      (URLs, JSON), animations (spinner, machine à écrire), streaming de
      `status`/`result` ; `NO_COLOR` et sorties non-TTY respectées.
- [ ] inference-translator phase 4 : bout en bout avec l'orchestrateur
      (dépend des phases 1–3 des deux côtés).
- [ ] Acceptation : rejouer une tâche de la phase 3 orchestrator depuis `/db`
      uniquement ; même prompt via l'IHM et via `tee`/`cat` purs.

## 7. P4 — Mode distant et boucle d'amélioration

- [ ] data-base-translator phase 5 : registre de clés SSH (`users`,
      `access_keys`, `auth_failures`), empreinte SHA-256, révocation par
      date, navigation `/db/users`, `/db/access_keys`.
- [ ] inference-translator phase 5 : `inference-serveur` par session SSH,
      trames `hello`/`request`/`status`/`result`/`error`, génération
      d'`authorized_keys` depuis le registre, `inference --remote`.
- [ ] Acceptation : même résultat en local et distant (seul `--remote`
      change) ; clé révoquée bloquée sans affecter les autres ; refus
      journalisés ; aucune clé privée dans la base.
- [ ] orchestrator-translator phase 5 : scores par instance, agrégation
      pondérée apprise, sélection automatique de topologies ; éventuels
      ajouts à neuron-translator (`stats`, graine).
- [ ] inference-translator phase 6 : historique de session, commandes
      `history`/`replay` côté client, cohérentes avec `/orchestrate`.

## 8. P5 — Durcissement et v1.0 (tous dépôts)

- [ ] httpfs-translator phase 4 : migration tests vers `libmicrohttpd` v2,
      multi-tâche/multi-utilisateur (un curseur par lecteur, verrous, pas
      d'état global sérialisé), optimisation Mach IPC (zéro copie).
- [ ] data-base-translator phase 6 : reconnexion automatique, lecture seule
      documentée, requêtes bornées, tests PostgreSQL jetable + charge,
      CI Hurd, docs ; **v1.0**.
- [ ] orchestrator-translator phase 6 : tests de charge (100+ instances),
      CI Hurd, docs ; **v1.0**.
- [ ] inference-translator phase 7 : tests pseudo-terminaux, sshd de test
      boucle locale, CI Hurd, docs (`interface.md`, `remote.md`,
      `architecture.md`) ; **v1.0**.

## 9. Règles transverses (rappels des PLAN.md)

- C23 / POSIX.1-2008, `trivfs` pour les MVP puis `netfs`.
- Harnais maison (`CHECK`, `make check`), zéro framework externe.
- Zéro allocation dans les chemins chauds.
- Erreurs SQL/HTTP ≠ erreurs POSIX : statuts lisibles (`duplicate`,
  `empty`, `invalid`, codes non-200).
- Contrats gelés en phase 0, revus en croisé entre dépôts ; un translator
  ne connaît jamais les binaires de ses voisins, seulement les points de
  montage.
- CI sous QEMU GNU/Hurd pilotée par `mistral-vm-debian-hurd`.
