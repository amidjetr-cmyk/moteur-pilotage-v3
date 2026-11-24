# moteur_v3 — moteur de calcul déterministe (pilotage patrimonial V3)

Ce dépôt ne contient **que du code**, sans aucune donnée personnelle : pas de position, de montant, de compte ni de paramètre privé.
Les paramètres personnels vivent dans le prompt de la tâche planifiée ; l'état vit dans un document privé.

## Utilisation dans une tâche planifiée

```sh
curl -fsSL "https://raw.githubusercontent.com/<compte>/<depot>/<commit>/moteur_v3.py" -o moteur_v3.py
sha256sum moteur_v3.py          # doit égaler l'empreinte inscrite dans le prompt
python3 -I moteur_v3.py selftest
python3 -I moteur_v3.py empreinte-params --params params.json --attendue <empreinte du prompt>
python3 -I moteur_v3.py executer --params params.json --empreinte-params <empreinte du prompt> --etat etat.json --collecte collecte.json --sortie sortie
python3 -I moteur_v3.py verifier-rapport --rapport rapport.md
python3 -I moteur_v3.py contenu-bloc --fichier sortie/etat.json          # chaîne à coller dans l'écriture du journal
python3 -I moteur_v3.py controle-bloc --fichier sortie/etat.json --h <h>  # contrôle de l'écriture, sans relecture
```

- Bibliothèque standard Python 3.10+ seulement ; aucun accès réseau.
- Toujours épingler un **commit** (jamais une branche) et vérifier l'empreinte SHA-256 avant exécution.
- Commandes : `selftest`, `version`, `calendrier`, `executer`, `confirmer`, `acquitter`, `verifier-etat`, `migrer-v2`, `empreinte-params`, `verifier-rapport`, `contenu-bloc`, `controle-bloc`.
- L'état est écrit en ASCII pur et scellé par une empreinte SHA-256 de son contenu : une recopie fautive est détectée à la lecture suivante.
- `executer` refuse des paramètres dont l'empreinte diffère de celle annoncée par le prompt.

## Ce qu'il ne faut pas publier

Les tests (`test_moteur_v3.py`) et les fichiers de rejeu utilisent des données réelles : ils restent privés.
