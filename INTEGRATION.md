# Note d'intégration — surface exacte

## Constat sur « le fork »

`~/Documents/gameshell/` n'est PAS un dépôt git : c'est l'extraction
**éphémère** de l'archive auto-extractible `~/Documents/gameshell.sh`
(GameShell v0.6.0-39-g53f470d + votre jeu de missions personnalisé :
`basic/`, `intermediate/`, `finding_files_maze/`, `pipe_intro_book_of_potions/`,
`pipes_merchant_stall/`, `stdin_stdout_stderr/`, `processes/`, `permissions/`,
`misc/`, `FINAL_MISSION` + générateurs `.sbin/`). À la sortie du jeu, ce
répertoire est resauvegardé en `gameshell-save-*.sh` puis **supprimé**
(`lib/header.sh:_remove_root`). La source durable de votre fork est donc
l'archive elle-même — c'est elle que `install.sh` patche (backup `.bak`).

## Fichiers AJOUTÉS dans le jeu (aucun fichier moteur modifié)

| Ajout | Mécanisme |
|---|---|
| `missions/tutor_hook/gshrc` (= `shim/gshrc_tutor.sh`) | format mission standard : `start.sh` copie tout fichier `gshrc` d'une mission vers `$GSH_CONFIG/gshrc_*.sh` à l'init, et `lib/gshrc` source ces fichiers à chaque session. Survit aux resets (l'init efface `.config`). |
| 1 ligne `!tutor_hook` en tête de `missions/default.idx` | entrée « dummy » standard (comme vos `!*/00_shared`) : ne consomme pas de numéro de mission. |
| `.config/gshrc_tutor.sh` (sauvegardes/dir vivant uniquement) | les parties continuées sautent l'init ; double chargement neutralisé par le garde `_TUTOR_ACTIVE`. |

Aucune modification de `lib/`, `start.sh`, ni d'aucune mission existante.
`default.idx` est le seul fichier existant touché (une ligne, réversible).

## Activation / désactivation

- Le shim est **inerte** sans `GSH_TUTOR=1` : `bash gameshell.sh` = jeu
  strictement normal (le shim se réduit à un `if` faux).
- `play.sh` exporte `GSH_TUTOR=1`, enregistre la sortie via `script(1)` et
  ouvre le panneau tuteur dans un split tmux.
- Désinstallation : restaurer `gameshell.sh.bak`, ou supprimer
  `missions/tutor_hook/` + la ligne `!tutor_hook` de `default.idx`.

## Côté tuteur (hors du jeu, dans ce dépôt)

- `shim/gshrc_tutor.sh` — SessionBridge côté shell : `trap DEBUG` (préexec) +
  premier/dernier élément du tableau `PROMPT_COMMAND` (postexec/armement),
  écrit `turns.jsonl`, marqueurs OSC invisibles pour trancher le typescript.
  bash uniquement (votre session est bash 5.1 ; zsh non couvert pour l'instant).
- `tutor/bridge.py` — assemble les tours complets (commande, code retour,
  sortie exacte, snapshot) ; lit `missions.log`/`index.idx` du moteur en
  lecture seule (traversée `--x` : lisibles par nom malgré la protection).
- `tutor/engine.py` — politique : quand parler, échelle d'aide, détection de
  blocage, mise à jour du modèle de l'apprenant.
- `tutor/llm.py` — `MockLLMClient` (hors-ligne, obligatoire) /
  `HttpLLMClient` (env `GSH_TUTOR_LLM_URL/_MODEL/_KEY`), règle maître dans le
  prompt système.
- `tutor/learner_model.py` — persisté en double : `$GSH_CONFIG/tutor/…`
  (voyage dans vos sauvegardes, colocalisé avec la progression du moteur) et
  `~/.local/share/gameshell-tutor/learner-<GSH_UID>.json` (survit à la
  suppression du répertoire) ; le plus récent gagne.
- Objectifs de mission : extraits par `install.sh` vers
  `~/.local/share/gameshell-tutor/goals-cache/` car `gsh protect` rend les
  missions illisibles PENDANT le jeu. Le contenu de `check.sh` n'est jamais
  transmis au LLM (intention seulement).

## Mode « Maître du Jeu » intégré (frontend par défaut)

- `tutor/tutor_daemon.py` : démon lancé par le shim au démarrage de session
  (sauf `GSH_TUTOR_FRONTEND=pane`). Même pipeline que le panneau, mais chaque
  message est rendu dans UN fichier de `$SESSION/outbox/` (écrit en
  tmp+rename : jamais de fichier partiel visible). Il s'arrête seul quand le
  shell du jeu meurt ou que le répertoire extrait disparaît.
- Livraison : le shim vide l'outbox depuis `PROMPT_COMMAND` (juste avant le
  prompt — jamais pendant une commande ni au milieu d'une frappe). Après un
  échec ou un `gsh check`, il attend jusqu'à ~2 s pour que le commentaire
  tombe sur CE prompt (instantané avec le mock).
- `gm` : écrit dans `$SESSION/chat.jsonl`, attend (jusqu'à 25 s) le fichier
  `*-reply-<id>.msg`, en vidant au passage les messages en attente. `gm` est
  exclu de l'enregistrement des tours (parler au tuteur n'est pas une
  activité shell).
- **Piège contourné — safe_rm** : le PATH du jeu remplace `rm` par un
  `safe_rm` qui refuse de toucher aux fichiers hors de l'arbre GameShell.
  Le shim supprime donc les messages livrés avec `command -p rm` (PATH
  système), avec repli « tronquer via builtin » (fichier vide = consommé,
  le démon fait le ménage).
- **Piège contourné — autosaves protégées** : une sauvegarde faite en cours
  de partie embarque les modes anti-triche (répertoires 0311 illisibles) ;
  `install.sh` normalise `u+rwX` sur l'arbre temporaire avant de le repaquer
  et refuse de remplacer l'archive si le repaquetage perd des entrées.

## Narration des missions (remplace le parchemin)

- Le moteur définit `_gsh_goal` comme un SCRIPT dans son PATH ; le shim le
  masque par une FONCTION du même nom (le dispatcher `gsh` résout les
  fonctions d'abord) — aucun fichier moteur touché, et `command _gsh_goal`
  atteint toujours l'original : utilisé par `gsh goal brut`, les appels avec
  numéro de mission explicite, et tout repli (démon absent, timeout).
- Le briefing (début de mission + `gsh goal`) est rendu par le gabarit
  déterministe du mock — texte de l'objectif verbatim, habillage en
  personnage — jamais par le LLM (un petit modèle local déforme les détails
  opérationnels ; constaté avec qwen2.5:3b). Mis en cache par mission+langue
  dans `goals-cache/narration-*.txt`.

## Détection automatique de la réussite

- Après chaque commande du joueur (hors `gsh`/`gm`), `_tutor_autocheck`
  évalue le prédicat de la mission : `( mission_source "$dir/check.sh" )
  </dev/null >/dev/null 2>&1` — le sous-shell confine les effets de bord des
  chemins d'échec (certains checks font `cd` ou `read`), l'absence de stdin
  fait échouer proprement les checks interactifs (jamais de question surprise).
- S'il passe, `_tutor_run_check` exécute le VRAI `gsh check` du moteur dans
  le shell principal, enveloppé dans un tour synthétique (marqueurs + events,
  numéro de mission capturé AVANT car la réussite fait avancer le jeu) : le
  démon le voit comme un check tapé — revue idiomatique, puis briefing
  immédiat de la mission suivante (dernier `START` de missions.log).
- Le verdict reste donc à 100 % celui du moteur ; le tuteur n'a fait
  qu'appuyer sur le bouton. `gm fini` donne le même chemin à la demande
  (checks interactifs). `GSH_HELP_HINT=never` coupe le rappel « gsh help ».

## Fluidité des livraisons

- Les genres « verdict » (briefing, victoire, échec, avertissements) sont
  TOUJOURS rendus par les gabarits instantanés (`FastVerdictClient` dans le
  démon) ; seuls la conversation `gm` et le diagnostic d'erreurs passent par
  le LLM. Victoire → briefing suivant tombent donc toujours dans la même
  fenêtre de prompt.
- Pendant qu'une réponse LLM se prépare, le démon pose un marqueur
  `$SESSION/pending` ; le hook de prompt du shim retient le prompt (indicateur
  « 🧙 … », plafond 20 s) au lieu de le rendre muet — un apprenant qui attend
  d'être guidé ne doit jamais fixer un prompt vide.
- Un push SIGUSR1 existe en bonus mais bash 5.1 DIFFÈRE les traps de signaux
  quand un trap DEBUG est actif (constaté empiriquement : livraison au retour
  de readline seulement) — d'où le choix du marqueur `pending` comme
  mécanisme principal.

## Points de vigilance connus

- Seuils d'escalade des indices : constantes `UNLOCK` dans `tutor/engine.py`.
- Le snapshot par commande (`find -maxdepth 4 | head -120` sur `$GSH_HOME`)
  coûte quelques ms par prompt — négligeable sur ce monde.
- Programmes plein écran (nano, less) : la tranche de typescript est du
  « bruit terminal » tronqué ; le tuteur n'essaie pas de l'interpréter.
- `intermediate/04_bg_xeyes` installe son propre `gshrc_*` et
  `pipes_merchant_stall` enveloppe `PS1` : compatibles (le shim est sourcé en
  premier par ordre alphabétique et ne touche pas `PS1`).
