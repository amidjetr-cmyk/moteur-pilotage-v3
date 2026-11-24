#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
moteur_v3.py — Moteur de calcul déterministe du pilotage patrimonial V3.

Principes
- Bibliothèque standard Python (3.10+) uniquement ; aucun accès réseau.
- Aucune donnée personnelle dans ce fichier : tout provient de params.json,
  etat.json et collecte.json, fournis à chaque exécution.
- Toute grandeur publiée dans un rapport sort de ce moteur. L'agent collecte,
  interprète les textes et rédige ; il ne recalcule pas.
- Les états destinés au journal sont écrits en ASCII pur (contenu-bloc) et
  l'écriture est contrôlée sans relecture (controle-bloc) : l'empreinte courte
  du bloc renvoyée par le document est comparée à celle du texte attendu.

Commandes
  selftest        vecteurs de contrôle internes (à lancer avant tout usage)
  calendrier      séance de référence, mode d'exécution, séances manquées
  executer        pipeline complet : validation, mesures, règles, diagnostic,
                  scénarios, notifications, nouvel état, brouillon de rapport
  confirmer       enregistre le résultat d'un envoi de notification
  acquitter       enregistre l'acquittement d'événements par l'utilisateur
  verifier-etat   contrôle d'intégrité et de schéma d'un état
  migrer-v2       construit l'état V3 initial à partir d'un extrait du journal V2
  contenu-bloc    chaîne JSON du bloc d'état, prête à coller dans l'appel d'écriture du journal
  controle-bloc   contrôle de l'écriture par l'empreinte courte renvoyée par le document
  version         affiche la version et l'empreinte du fichier
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import random
import re
import statistics
import sys
from zoneinfo import ZoneInfo

VERSION = "3.0.0-rc4"
SCHEMA_ETAT = "etat-pilotage-v3"
VERSION_SCHEMA_ETAT = "3.0.0"
SCHEMA_PARAMS = "params-pilotage-v3"

PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
UN_JOUR = dt.timedelta(days=1)


# Paramètres génériques (aucune donnée personnelle) : scénarios, hypothèses de
# stress provisoires (P-09) et contrôles. Le bloc privé du prompt peut les surcharger.
DEFAUTS_PUBLICS = {
 "scenarios": [
  {
   "id": "S1",
   "libelle": "Correction modérée — actions −10 %",
   "modele": "global",
   "chocs": {
    "actions": -0.1
   }
  },
  {
   "id": "S2",
   "libelle": "Correction importante — actions −20 %",
   "modele": "global",
   "chocs": {
    "actions": -0.2
   }
  },
  {
   "id": "S3",
   "libelle": "Crise sévère — actions −30 %",
   "modele": "global",
   "chocs": {
    "actions": -0.3
   }
  },
  {
   "id": "S4",
   "libelle": "Crise technologique — tech −30 %, reste −10 %",
   "modele": "tech",
   "chocs": {
    "tech": -0.3,
    "reste": -0.1
   }
  },
  {
   "id": "S5",
   "libelle": "Crise logicielle — logiciels −40 %, autre tech −15 %, reste −5 %",
   "modele": "logiciel",
   "chocs": {
    "logiciel": -0.4,
    "tech_hors_logiciel": -0.15,
    "reste": -0.05
   }
  },
  {
   "id": "S6",
   "libelle": "Choc extrême — actions −40 %",
   "modele": "global",
   "chocs": {
    "actions": -0.4
   }
  }
 ],
 "hypotheses_stress": {
  "statut_validation": "PROVISOIRE_V3 (P-09)",
  "beta_defaut": 1.0,
  "facteur_crypto": 2.0,
  "choc_formule_garantie": 0.0,
  "part_tech_max_inconnue": 0.4,
  "part_logiciel_max_inconnue": 0.12,
  "scenario_reference": "S3",
  "traduction_nasdaq": "« Nasdaq −30 % » traduit en technologies de l'information + services de communication −30 %, reste du marché −10 %"
 },
 "controles": {
  "ecart_lignes_compte_max": 0.005,
  "fraicheur_max_heures": 30,
  "saut_denominateur_max": 0.03,
  "fraicheur_max_heures_par_compte": {}
 }
}


def charger_params(prives: dict) -> dict:
    """Fusionne les paramètres génériques du moteur et le bloc privé du prompt."""
    p = copy.deepcopy(DEFAUTS_PUBLICS)
    for k, v in (prives or {}).items():
        if isinstance(v, dict) and isinstance(p.get(k), dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


# ════════════════════════════════════════════════════════════════════════════
# 1. Utilitaires
# ════════════════════════════════════════════════════════════════════════════

def json_canonique(obj) -> str:
    """Sérialisation canonique : clés triées, sans espaces, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def empreinte(obj, exclure=("empreinte",)) -> str:
    """SHA-256 du JSON canonique (insensible à la mise en forme)."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k not in exclure}
    return hashlib.sha256(json_canonique(obj).encode("utf-8")).hexdigest()


def empreinte_fichier(chemin) -> str:
    with open(chemin, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def lire_date(x) -> dt.date | None:
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return dt.date.fromisoformat(str(x)[:10])


def lire_instant(x) -> dt.datetime:
    if isinstance(x, dt.datetime):
        if x.tzinfo is None:
            raise ValueError("instant sans fuseau")
        return x
    s = str(x).replace("Z", "+00:00")
    v = dt.datetime.fromisoformat(s)
    if v.tzinfo is None:
        raise ValueError(f"instant sans fuseau : {x}")
    return v


def fmt_nombre(x, n=2) -> str:
    if x is None:
        return "n.d."
    s = f"{abs(x):,.{n}f}".replace(",", " ").replace(".", ",")
    return ("−" if x < 0 and round(abs(x), n) != 0 else "") + s


def fmt_eur(x, n=2) -> str:
    return "n.d." if x is None else fmt_nombre(x, n) + " €"


def fmt_date(d, annee=True) -> str:
    x = lire_date(d)
    return "—" if x is None else x.strftime("%d/%m/%Y" if annee else "%d/%m")


def fmt_pct(x, n=2) -> str:
    return "n.d." if x is None else fmt_nombre(x * 100, n) + " %"


def fmt_pt(x, n=2) -> str:
    return "n.d." if x is None else fmt_nombre(x * 100, n) + " pt"


def ceil_strict(x: float) -> int:
    """Arrondi supérieur tolérant aux erreurs de flottant (8,0000000001 → 8)."""
    return int(math.ceil(round(x, 9)))


def floor_strict(x: float) -> int:
    return int(math.floor(round(x, 9)))


# ════════════════════════════════════════════════════════════════════════════
# 2. Calendrier des marchés et horloge
# ════════════════════════════════════════════════════════════════════════════

def paques(annee: int) -> dt.date:
    """Dimanche de Pâques (algorithme grégorien anonyme)."""
    a = annee % 19
    b, c = divmod(annee, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mois = (h + l - 7 * m + 114) // 31
    jour = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(annee, mois, jour)


def _neme(annee, mois, jour_semaine, n) -> dt.date:
    d = dt.date(annee, mois, 1)
    d += dt.timedelta(days=(jour_semaine - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _dernier(annee, mois, jour_semaine) -> dt.date:
    d = dt.date(annee + (mois == 12), mois % 12 + 1, 1) - UN_JOUR
    return d - dt.timedelta(days=(d.weekday() - jour_semaine) % 7)


def _observe_us(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        return d - UN_JOUR
    if d.weekday() == 6:
        return d + UN_JOUR
    return d


def feries_nyse(annee: int) -> set:
    """Jours fériés réguliers du NYSE. Les fermetures exceptionnelles
    (deuil national, incident) ne sont pas prévisibles : l'agent les vérifie."""
    f = set()
    nouvel_an = dt.date(annee, 1, 1)
    if nouvel_an.weekday() == 6:
        f.add(dt.date(annee, 1, 2))
    elif nouvel_an.weekday() < 5:
        f.add(nouvel_an)          # samedi : pas de report au 31/12 (règle NYSE)
    f.add(_neme(annee, 1, 0, 3))   # Martin Luther King
    f.add(_neme(annee, 2, 0, 3))   # Presidents' Day
    f.add(paques(annee) - dt.timedelta(days=2))  # Vendredi saint
    f.add(_dernier(annee, 5, 0))   # Memorial Day
    if annee >= 2022:
        f.add(_observe_us(dt.date(annee, 6, 19)))  # Juneteenth
    f.add(_observe_us(dt.date(annee, 7, 4)))
    f.add(_neme(annee, 9, 0, 1))   # Labor Day
    f.add(_neme(annee, 11, 3, 4))  # Thanksgiving
    f.add(_observe_us(dt.date(annee, 12, 25)))
    return f


def fermetures_anticipees_nyse(annee: int) -> set:
    """Séances écourtées à 13 h (New York)."""
    s = set()
    feries = feries_nyse(annee)
    j3 = dt.date(annee, 7, 3)
    if j3.weekday() < 4 and j3 not in feries:
        s.add(j3)
    s.add(_neme(annee, 11, 3, 4) + UN_JOUR)  # lendemain de Thanksgiving
    v24 = dt.date(annee, 12, 24)
    if v24.weekday() < 4:
        s.add(v24)
    return s


def feries_euronext(annee: int) -> set:
    p = paques(annee)
    cands = {dt.date(annee, 1, 1), p - dt.timedelta(days=2), p + UN_JOUR,
             dt.date(annee, 5, 1), dt.date(annee, 12, 25), dt.date(annee, 12, 26)}
    return {d for d in cands if d.weekday() < 5}


def fermetures_anticipees_euronext(annee: int) -> set:
    return {d for d in (dt.date(annee, 12, 24), dt.date(annee, 12, 31)) if d.weekday() < 5}


def est_jour_fixing_bce(d: dt.date) -> bool:
    """La BCE ne publie pas de taux de référence les jours de fermeture TARGET."""
    return d.weekday() < 5 and d not in feries_euronext(d.year)


def est_seance(d: dt.date, marche: str = "XNYS") -> bool:
    if d.weekday() >= 5:
        return False
    if marche == "XNYS":
        return d not in feries_nyse(d.year)
    if marche == "XPAR":
        return d not in feries_euronext(d.year)
    raise ValueError(marche)


def heure_cloture(d: dt.date, marche: str = "XNYS") -> dt.datetime:
    """Heure de clôture de la séance, en UTC (changements d'heure gérés)."""
    if marche == "XNYS":
        h, m = (13, 0) if d in fermetures_anticipees_nyse(d.year) else (16, 0)
        return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=NEW_YORK).astimezone(UTC)
    if marche == "XPAR":
        h, m = (14, 5) if d in fermetures_anticipees_euronext(d.year) else (17, 30)
        return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=PARIS).astimezone(UTC)
    raise ValueError(marche)


def seance_precedente(d: dt.date, marche="XNYS") -> dt.date:
    x = d - UN_JOUR
    while not est_seance(x, marche):
        x -= UN_JOUR
    return x


def seance_suivante(d: dt.date, marche="XNYS") -> dt.date:
    x = d + UN_JOUR
    while not est_seance(x, marche):
        x += UN_JOUR
    return x


def derniere_seance_cloturee(maintenant: dt.datetime, marche="XNYS", marge_min=15) -> dt.date:
    """Dernière séance dont la clôture (+ marge de publication) est passée."""
    m = lire_instant(maintenant).astimezone(UTC)
    tz = NEW_YORK if marche == "XNYS" else PARIS
    d = m.astimezone(tz).date()
    for _ in range(15):
        if est_seance(d, marche) and m >= heure_cloture(d, marche) + dt.timedelta(minutes=marge_min):
            return d
        d -= UN_JOUR
    raise RuntimeError("aucune séance trouvée sur 15 jours")


def seances_entre(apres: dt.date | None, jusqua: dt.date, marche="XNYS", limite=30) -> list:
    """Séances strictement après `apres` et jusqu'à `jusqua` incluse."""
    if apres is None:
        return [jusqua]
    out, d = [], jusqua
    while d > apres and len(out) < limite:
        if est_seance(d, marche):
            out.append(d)
        d -= UN_JOUR
    return sorted(out)


def determiner_mode(maintenant, derniere_officielle=None, verrou=None, run_id=None,
                    marge_min=15, verrou_minutes=45) -> dict:
    """Mode d'exécution : OFFICIEL, RATTRAPAGE, ALLEGE ou CONTROLE (+ hebdo)."""
    m = lire_instant(maintenant).astimezone(UTC)
    cible = derniere_seance_cloturee(m, "XNYS", marge_min)
    jour_ny = m.astimezone(NEW_YORK).date()
    jour_paris = m.astimezone(PARIS).date()
    dernier = lire_date(derniere_officielle)
    manquees = seances_entre(dernier, cible)
    raison = ""
    if verrou and verrou.get("run_id") and verrou.get("run_id") != run_id:
        age = (m - lire_instant(verrou["depuis"])).total_seconds() / 60
        if age < verrou_minutes:
            mode, raison = "CONTROLE", f"verrou détenu par {verrou['run_id']} depuis {age:.0f} min"
        else:
            mode, raison = None, f"verrou expiré ({age:.0f} min) ignoré"
    else:
        mode = None
    if mode is None:
        if dernier is not None and dernier >= cible:
            if jour_ny.weekday() < 5 and not est_seance(jour_ny):
                mode, raison = "ALLEGE", raison or "jour férié US : séance déjà traitée"
            else:
                mode, raison = "CONTROLE", raison or "séance cible déjà traitée"
        elif len(manquees) > 1:
            mode, raison = "RATTRAPAGE", f"{len(manquees) - 1} séance(s) manquée(s)"
        else:
            mode, raison = "OFFICIEL", raison or "séance cible non traitée"
    avant_cloture = est_seance(jour_ny) and m < heure_cloture(jour_ny) + dt.timedelta(minutes=marge_min)
    return {
        "maintenant_utc": m.isoformat(),
        "maintenant_paris": m.astimezone(PARIS).isoformat(),
        "seance_cible": cible.isoformat(),
        "donnees_j_moins_1": bool(avant_cloture),
        "mode": mode,
        "raison": raison,
        "seances_a_traiter": [d.isoformat() for d in manquees] if mode in ("OFFICIEL", "RATTRAPAGE") else [],
        "hebdomadaire": jour_paris.weekday() == 0 and mode in ("OFFICIEL", "RATTRAPAGE", "ALLEGE"),
        "jour_ferie_us": jour_ny.weekday() < 5 and not est_seance(jour_ny),
        "seance_ecourtee": cible in fermetures_anticipees_nyse(cible.year),
        "prochaine_seance": seance_suivante(cible).isoformat(),
    }


# ════════════════════════════════════════════════════════════════════════════
# 3. Qualité des données
# ════════════════════════════════════════════════════════════════════════════

ORDRE_QUALITE = {"VALIDE": 5, "PROBABLE": 4, "DOUTEUSE": 2, "DIVERGENTE": 1, "REJETEE": 0, "MANQUANTE": 0}
# Les taux sont exprimés en points de pourcentage (4,95 = 4,95 %) : tolérance 3 pb, borne 40 pb.
TOLERANCE = {"indice": 0.001, "change": 0.0005, "action": 0.003, "etf": 0.003,
             "taux": 0.03, "matiere": 0.015, "volatilite": 0.015, "fonds": 0.003}
BORNE_VARIATION = {"indice": 0.08, "change": 0.03, "action": 0.25, "etf": 0.10,
                   "taux": 0.40, "matiere": 0.15, "volatilite": 0.60, "fonds": 0.10}
NATURES_ABSOLUES = {"taux"}


def valider_observations(observations, cle, date_cible, nature, precedent=None,
                         anteriorite_max_jours=0, exigences=None) -> dict:
    """Agrège les observations d'une donnée et lui attribue une qualité.

    VALIDE     ≥ 2 sources distinctes concordantes (tolérance par nature)
    PROBABLE   1 source primaire, Finary, ou secondaire citée textuellement
    DOUTEUSE   1 source secondaire résumée par un modèle, ou variation hors bornes
    DIVERGENTE sources en désaccord au-delà de la tolérance
    REJETEE    aucune observation à la bonne date (ou page non datée)
    MANQUANTE  aucune observation

    `exigences` (paramètres de l'instrument) reprend le recoupement de la V2 §2 :
    sources_min (2 pour les données que la V2 recoupe), primaire_requise, tolerance
    (« les deux identiques » : au cent près). Une exigence non remplie
    laisse la qualité affichée mais pose `recoupement_insuffisant` : aucune
    transition d'état n'est alors possible."""
    exigences = exigences or {}
    dc = lire_date(date_cible)
    cands = [o for o in observations if o.get("cle") == cle]
    res = {"cle": cle, "date_cible": dc.isoformat() if dc else None, "valeur": None,
           "qualite": "MANQUANTE", "sources": [], "ecart": None, "drapeaux": []}
    if not cands:
        return res
    ok, rejets = [], []
    for o in cands:
        d = lire_date(o.get("date_obs"))
        if d is None:
            rejets.append("page non datée : " + str(o.get("source_nom")))
        elif dc is not None and d != dc and not (d < dc and (dc - d).days <= anteriorite_max_jours):
            rejets.append(f"date {d} ≠ séance {dc} ({o.get('source_nom')})")
        else:
            ok.append(o)
    res["drapeaux"].extend(rejets)
    if not ok:
        res["qualite"] = "REJETEE"
        return res
    valeurs = [float(o["valeur"]) for o in ok]
    noms = sorted({str(o.get("source_nom")) for o in ok})
    res["sources"] = [{"nom": o.get("source_nom"), "type": o.get("source_type"), "url": o.get("url"),
                       "extraction": o.get("extraction"), "valeur": o.get("valeur"),
                       "date_obs": o.get("date_obs")} for o in ok]
    med = statistics.median(valeurs)
    ecart = (max(valeurs) - min(valeurs)) if nature in NATURES_ABSOLUES else (
        (max(valeurs) - min(valeurs)) / abs(med) if med else 0.0)
    res["ecart"] = ecart
    tol = exigences.get("tolerance", TOLERANCE.get(nature, 0.003))
    primaires = [o for o in ok if o.get("source_type") in ("primaire", "finary")]
    if len(noms) >= 2:
        if ecart <= tol + 1e-12:
            res["qualite"] = "VALIDE"
            res["valeur"] = float(primaires[0]["valeur"]) if primaires else med
        else:
            res["qualite"] = "DIVERGENTE"
            res["drapeaux"].append(f"écart {fmt_pct(ecart, 3)} > tolérance {fmt_pct(tol, 3)}" if nature not in NATURES_ABSOLUES
                                   else f"écart {fmt_nombre(ecart * 100, 1)} pb > tolérance {fmt_nombre(tol * 100, 1)} pb")
            return res
    else:
        o = ok[0]
        res["valeur"] = float(o["valeur"])
        t, ext = o.get("source_type"), o.get("extraction", "extrait_textuel")
        if t in ("primaire", "finary", "utilisateur"):
            res["qualite"] = "PROBABLE"
        elif t == "secondaire" and ext != "resume_modele":
            res["qualite"] = "PROBABLE"
        else:
            res["qualite"] = "DOUTEUSE"
    retenue = next((o for o in ok if o.get("source_type") in ("primaire", "finary")), ok[0])
    res["date_retenue"] = lire_date(retenue.get("date_obs")).isoformat()
    if any(lire_date(o.get("date_obs")) != dc for o in ok):
        res["drapeaux"].append("valeur antérieure à la séance cible (report autorisé)")
    manque = []
    if len(noms) < int(exigences.get("sources_min", 1)):
        manque.append(f"{len(noms)} source(s) sur {exigences['sources_min']} exigées par la V2")
    if exigences.get("primaire_requise") and not primaires:
        manque.append("source primaire exigée par la V2")
        if res["qualite"] == "VALIDE":
            res["qualite"] = "PROBABLE"
    if manque:
        res["recoupement_insuffisant"] = " ; ".join(manque)
        res["drapeaux"].append("recoupement insuffisant : " + res["recoupement_insuffisant"])
    if precedent not in (None, 0) and res["valeur"] is not None:
        var = (res["valeur"] - precedent) if nature in NATURES_ABSOLUES else (res["valeur"] / precedent - 1)
        res["variation"] = var
        if abs(var) > BORNE_VARIATION.get(nature, 0.25):
            res["drapeaux"].append("variation hors bornes de vraisemblance")
            if res["qualite"] != "VALIDE":
                res["qualite"] = "DOUTEUSE"
    return res


def qualite_suffit_pour_transition(mesure: dict | None) -> bool:
    """Une transition d'état exige VALIDE, ou PROBABLE issue d'une source primaire.
    Pour une mesure composée (indice ÷ change), chaque composante doit suffire."""
    if not mesure:
        return False
    if mesure.get("composantes"):
        return all(qualite_suffit_pour_transition(c) for c in mesure["composantes"])
    if mesure.get("recoupement_insuffisant"):
        return False
    q = mesure.get("qualite")
    if q == "VALIDE":
        return True
    if q == "PROBABLE":
        return any(s.get("type") in ("primaire", "finary") and s.get("extraction") != "resume_modele"
                   for s in mesure.get("sources", []))
    return False


def cles_a_verifier(mesure: dict, defaut: list) -> list:
    """Les composantes d'une mesure qui empêchent une transition (à collecter de nouveau)."""
    comps = mesure.get("composantes") or []
    k = [c.get("cle") for c in comps if c.get("cle") and not qualite_suffit_pour_transition(c)]
    return k or mesure.get("cles_source", defaut)


def qualite_combinee(*mesures) -> str:
    qs = [m.get("qualite", "MANQUANTE") if m else "MANQUANTE" for m in mesures]
    return min(qs, key=lambda q: ORDRE_QUALITE.get(q, 0))


# ════════════════════════════════════════════════════════════════════════════
# 4. Drawdowns et plus hauts
# ════════════════════════════════════════════════════════════════════════════

def fixing_a_date(fixings: dict, d: dt.date, ecart_max_jours: int = 5):
    """Dernier fixing BCE disponible à la date d (jointure « as-of »)."""
    x = lire_date(d)
    for _ in range(ecart_max_jours + 1):
        k = x.isoformat()
        if k in fixings:
            return float(fixings[k]), x
        x -= UN_JOUR
    return None, None


def construire_serie_eur(clotures_usd: dict, fixings_usd_par_eur: dict) -> list:
    """Série en EUR : valeur_EUR = clôture_USD ÷ (USD pour 1 EUR, fixing BCE du jour
    ou dernier fixing antérieur si la BCE n'a pas publié)."""
    out = []
    for k in sorted(clotures_usd):
        fx, dfx = fixing_a_date(fixings_usd_par_eur, lire_date(k))
        if fx is None:
            out.append({"date": k, "usd": clotures_usd[k], "fx": None, "eur": None, "fx_decale": None})
            continue
        out.append({"date": k, "usd": float(clotures_usd[k]), "fx": fx, "fx_date": dfx.isoformat(),
                    "eur": float(clotures_usd[k]) / fx, "fx_decale": dfx.isoformat() != k})
    return out


def plus_haut(points: list, cle: str = "v"):
    """(valeur max, date) — première occurrence du maximum."""
    best = None
    for p in points:
        v = p.get(cle)
        if v is None:
            continue
        if best is None or v > best[0] + 1e-12:
            best = (v, p["date"])
    return best


def drawdown(valeur: float, hwm: float) -> float:
    return valeur / hwm - 1.0


def drawdown_eur_formule_v2(cloture_usd, hwm_usd, fx_au_plus_haut, fx_jour) -> float:
    """Formule V2 : (1 + DD_USD) × (FX au plus haut USD ÷ FX du jour) − 1.
    Elle mesure l'écart à la valeur en EUR du jour du plus haut en USD, pas au
    vrai plus haut en EUR : elle ne peut que sous-estimer la baisse."""
    return (cloture_usd / hwm_usd) * (fx_au_plus_haut / fx_jour) - 1.0


def maj_plus_haut(hwm: dict | None, date_obs, valeur: float) -> dict:
    """Mise à jour incrémentale d'un plus haut de clôture."""
    d = lire_date(date_obs).isoformat()
    if hwm is None or hwm.get("valeur") is None or valeur > hwm["valeur"] + 1e-12:
        nv = dict(hwm or {})
        nv.update({"valeur": valeur, "date": d, "complet": bool((hwm or {}).get("complet"))})
        nv.pop("note", None)
        return nv
    return dict(hwm)


def plus_haut_glissant(points: list, date_ref, jours: int = 365, cle: str = "v"):
    """Plus haut sur la fenêtre ]date_ref − jours ; date_ref] (52 semaines ≈ 365 jours)."""
    dref = lire_date(date_ref)
    debut = dref - dt.timedelta(days=jours)
    fen = [p for p in points if debut < lire_date(p["date"]) <= dref and p.get(cle) is not None]
    return plus_haut(fen, cle)


def maj_fenetre_max(file: list | None, date_obs, valeur: float, jours: int = 365) -> list:
    """Plus haut glissant incrémental (file monotone décroissante) : on ne garde que
    les points qui peuvent encore devenir le maximum. Le maximum est file[0]."""
    d = lire_date(date_obs)
    f = [p for p in (file or []) if lire_date(p["date"]) > d - dt.timedelta(days=jours)]
    while f and f[-1]["v"] <= valeur:
        f.pop()
    f.append({"date": d.isoformat(), "v": valeur})
    return f


def maj_indice_twr(acc: dict | None, date_obs, fa: float, flux_externes: float = 0.0, garder: int = 260) -> dict:
    """Accumulateur de performance pondérée par le temps, mis à jour à chaque séance :
    I_t = I_{t−1} × (FA_t − F_t) / FA_{t−1}, où F_t = flux externes nets de la période
    (versements > 0). Suit aussi le plus haut de l'indice et la perte maximale."""
    d = lire_date(date_obs).isoformat()
    if not acc or not acc.get("fa"):
        return {"date": d, "fa": fa, "indice": 1.0, "pic": 1.0, "date_pic": d, "mdd": 0.0,
                "debut_annee": {"annee": int(d[:4]), "indice": 1.0}, "rendements": [], "flux_cumules": 0.0}
    if d <= acc["date"]:
        return acc
    a = dict(acc)
    r = (fa - flux_externes) / acc["fa"] - 1
    a["indice"] = acc["indice"] * (1 + r)
    a["rendements"] = (list(acc.get("rendements", [])) + [round(r, 7)])[-garder:]
    a["flux_cumules"] = acc.get("flux_cumules", 0.0) + flux_externes
    if a["indice"] > acc["pic"]:
        a["pic"], a["date_pic"] = a["indice"], d
    a["mdd"] = min(acc["mdd"], a["indice"] / a["pic"] - 1)
    if int(d[:4]) != acc["debut_annee"]["annee"]:
        a["debut_annee"] = {"annee": int(d[:4]), "indice": acc["indice"]}
    a["date"], a["fa"] = d, fa
    return a


def max_drawdown(points: list, cle: str = "v") -> dict:
    """Perte maximale pic-creux sur une série d'indice (TWR), jamais sur une valeur brute."""
    pic, dpic, mdd, res = None, None, 0.0, {"mdd": 0.0, "pic": None, "creux": None}
    for p in points:
        v = p.get(cle)
        if v is None:
            continue
        if pic is None or v > pic:
            pic, dpic = v, p["date"]
        dd = v / pic - 1
        if dd < mdd:
            mdd = dd
            res = {"mdd": mdd, "pic": dpic, "creux": p["date"]}
    return res


# ════════════════════════════════════════════════════════════════════════════
# 5. Poids, seuils et ordres
# ════════════════════════════════════════════════════════════════════════════

def poids(valeur: float, denominateur: float) -> float:
    if not denominateur:
        raise ValueError("dénominateur nul")
    return valeur / denominateur


def poids_recalcule(quantite, cours_devise, usd_par_eur, valeur_ligne_finary, fa_finary) -> dict:
    """Revalorise une ligne au cours de clôture et corrige le dénominateur d'autant."""
    v = quantite * cours_devise / usd_par_eur
    d = fa_finary - valeur_ligne_finary + v
    return {"valeur_recalculee": v, "denominateur_recalcule": d, "poids": v / d,
            "poids_finary": valeur_ligne_finary / fa_finary,
            "ecart_valeur": v - valeur_ligne_finary, "cours_eur": cours_devise / usd_par_eur}


def prix_seuil(quantite, poids_seuil, reste) -> float:
    """Cours (EUR) auquel la ligne atteint le poids seuil, le reste du patrimoine
    étant inchangé : q·P / (R + q·P) = w  ⇔  P = w·R / (q·(1 − w))."""
    return poids_seuil * reste / (quantite * (1 - poids_seuil))


def prix_seuil_formule_v2(quantite, poids_seuil, denominateur) -> float:
    """Approximation V2 « à dénominateur constant » : P = w·D / q (biaisée vers le bas)."""
    return poids_seuil * denominateur / quantite


def titres_a_vendre(valeur, cours, poids_cible, denominateur, produit_reste_dans_fa=True) -> int:
    """Nombre de titres à céder pour revenir au poids cible (arrondi supérieur).
    Produit conservé dans les actifs financiers : N = (V − w·D) / P.
    Produit sorti des actifs financiers : N = (V − w·D) / (P·(1 − w))."""
    exces = valeur - poids_cible * denominateur
    if exces <= 0:
        return 0
    div = cours if produit_reste_dans_fa else cours * (1 - poids_cible)
    return ceil_strict(exces / div)


def titres_a_acheter(valeur, cours, poids_cible, denominateur, financement_hors_fa=False) -> int:
    """Nombre de titres pour atteindre le poids cible sans le dépasser (arrondi inférieur).
    Financement interne aux actifs financiers : N = (w·D − V) / P.
    Financement externe (compte courant) : N = (w·D − V) / (P·(1 − w))."""
    manque = poids_cible * denominateur - valeur
    if manque <= 0:
        return 0
    div = cours * (1 - poids_cible) if financement_hors_fa else cours
    return floor_strict(manque / div)


def decomposer_derive_poids(v0, r0, v1, r1) -> dict:
    """Décomposition exacte (Shapley) de la variation du poids w = V / (V + R)
    entre l'effet de la ligne et l'effet du reste du patrimoine.
    La V2 donnait w(V1,R0) et w(V0,R1), dont les écarts ne s'additionnent pas."""
    def w(v, r):
        return v / (v + r)
    total = w(v1, r1) - w(v0, r0)
    ligne = 0.5 * ((w(v1, r0) - w(v0, r0)) + (w(v1, r1) - w(v0, r1)))
    reste = 0.5 * ((w(v0, r1) - w(v0, r0)) + (w(v1, r1) - w(v1, r0)))
    return {"poids_initial": w(v0, r0), "poids_final": w(v1, r1), "variation": total,
            "effet_ligne": ligne, "effet_reste": reste,
            "v2_seule_ligne": w(v1, r0), "v2_seul_reste": w(v0, r1),
            "residu_v2": total - ((w(v1, r0) - w(v0, r0)) + (w(v0, r1) - w(v0, r0)))}


def decomposer_reste(r0, r1, flux_externes) -> dict:
    """Variation du reste = effet de marché + flux externes (versements, retraits,
    virements depuis ou vers le compte courant, hors actifs financiers)."""
    return {"variation": r1 - r0, "flux_externes": flux_externes, "effet_marche": r1 - r0 - flux_externes}


# ════════════════════════════════════════════════════════════════════════════
# 6. Performance
# ════════════════════════════════════════════════════════════════════════════

def rendement_dietz(v0, v1, flux) -> float:
    """Dietz modifié. flux = [(fraction écoulée de la période ∈ [0,1], montant)]
    (montant > 0 = versement entrant)."""
    somme = sum(f for _, f in flux)
    pond = sum((1 - t) * f for t, f in flux)
    return (v1 - v0 - somme) / (v0 + pond)


def twr(points) -> float:
    """Rendement pondéré par le temps. points = [(date, valeur_avant_flux, flux_apres_valorisation)].
    TWR = Π V_k / (V_{k−1} + F_{k−1}) − 1."""
    if len(points) < 2:
        raise ValueError("au moins deux valorisations")
    prod = 1.0
    for (_, v_prec, f_prec), (_, v, _) in zip(points, points[1:]):
        prod *= v / (v_prec + f_prec)
    return prod - 1.0


def xirr(flux_dates, tol=1e-10) -> float:
    """TRI (rendement pondéré par les capitaux). flux_dates = [(date, montant)] vus
    de l'investisseur : versements < 0, valeur finale > 0. Bissection déterministe."""
    pts = [(lire_date(d), m) for d, m in flux_dates]
    t0 = min(d for d, _ in pts)

    def van(r):
        return sum(m / (1 + r) ** ((d - t0).days / 365.0) for d, m in pts)
    bas, haut = -0.99, 10.0
    vb, vh = van(bas), van(haut)
    if vb * vh > 0:
        raise ValueError("TRI non encadré")
    for _ in range(300):
        mil = (bas + haut) / 2
        vm = van(mil)
        if abs(vm) < tol:
            return mil
        if vb * vm < 0:
            haut, vh = mil, vm
        else:
            bas, vb = mil, vm
    return (bas + haut) / 2


def annualiser(rendement, jours) -> float | None:
    """Annualisation refusée sous un an : un rendement court annualisé trompe."""
    if jours < 365:
        return None
    return (1 + rendement) ** (365.25 / jours) - 1


def decomposer_variation_patrimoine(p0, p1, flux_nets) -> dict:
    """Δ patrimoine = flux nets externes + gain financier."""
    return {"variation": p1 - p0, "flux_nets": flux_nets, "gain_financier": p1 - p0 - flux_nets}


def contributions(lignes0: dict, lignes1: dict, flux: dict | None = None) -> dict:
    """Contribution de chaque ligne à la performance d'une période sans arbitrage :
    c_i = (V_i1 − V_i0 − F_i) / V_total0."""
    flux = flux or {}
    tot0 = sum(lignes0.values())
    return {k: (lignes1.get(k, 0.0) - v0 - flux.get(k, 0.0)) / tot0 for k, v0 in lignes0.items()}


# ════════════════════════════════════════════════════════════════════════════
# 7. Transparence des fonds et concentration
# ════════════════════════════════════════════════════════════════════════════

def expositions_emetteurs(lignes: list, expos_fonds: dict) -> dict:
    """Exposition consolidée par émetteur : directe + via fonds.
    Pour un ETF synthétique, l'exposition doit être celle de l'indice (pas le panier
    de substitution). Si un fonds ne publie qu'une partie de ses lignes, l'exposition
    totale est un minimum (« ≥ »)."""
    out = {}
    fonds_partiels = []
    for l in lignes:
        v = float(l.get("valeur_eur") or 0)
        if l.get("type") == "action" and l.get("emetteur"):
            e = out.setdefault(l["emetteur"], {"direct": 0.0, "indirect": 0.0, "indirect_haut": 0.0, "detail": {}})
            e["direct"] += v
        elif l.get("type") in ("fonds", "etf") and l.get("expo"):
            fx = expos_fonds.get(l["expo"])
            if not fx:
                continue
            # part actions de la LIGNE (fonds mixte : fourchette, ex. 30–70 %), pas celle de l'indice (100 %)
            pa_bas, pa_haut, _ = _bornes(l.get("part_actions", fx.get("part_actions", 1.0)), 1.0)
            part_actions = pa_bas                 # minimum défendable
            if fx.get("replication") == "synthetique" and fx.get("base") == "panier":
                raise ValueError(f"{l['expo']} : ETF synthétique décrit par son panier de substitution")
            for em, w in fx.get("emetteurs", {}).items():
                e = out.setdefault(em, {"direct": 0.0, "indirect": 0.0, "indirect_haut": 0.0, "detail": {}})
                montant = v * part_actions * w
                e["indirect"] += montant
                e["indirect_haut"] += v * pa_haut * w
                e["detail"][l["id"]] = e["detail"].get(l["id"], 0.0) + montant
            if pa_haut > pa_bas:
                fonds_partiels.append(l["id"])    # part actions en fourchette : total = minimum
            if fx.get("couverture_emetteurs", 1.0) < 0.999:
                fonds_partiels.append(l["id"])
    for em, e in out.items():
        e["total_min"] = e["direct"] + e["indirect"]
        e["total_haut_connu"] = e["direct"] + e.get("indirect_haut", e["indirect"])
        e["minimum"] = bool(fonds_partiels)
    return {"emetteurs": out, "fonds_partiels": sorted(set(fonds_partiels))}


def repartition(lignes: list, expos_fonds: dict, dimension: str, actions_seulement: bool = True) -> dict:
    """Ventilation de l'exposition actions par pays ou secteur (ou de tout le
    patrimoine par devise si actions_seulement=False). La part non documentée
    reste visible (« non ventilé ») au lieu d'être répartie au prorata."""
    champ = {"pays": "pays", "secteurs": "secteur", "devises": "devise"}[dimension]
    out = {}

    def add(k, x):
        out[k] = out.get(k, 0.0) + x
    for l in lignes:
        v = float(l.get("valeur_eur") or 0)
        if v == 0:
            continue
        if l.get("type") not in ("action", "etf", "fonds", "non_cote"):
            if not actions_seulement:
                add(l.get(champ) or ("EUR" if dimension == "devises" else "non ventilé"), v)
            continue
        pa = _bornes(l.get("part_actions", 1.0), 1.0)[0]
        fx = expos_fonds.get(l.get("expo")) if l.get("expo") else None
        if fx and fx.get(dimension):
            vent = fx[dimension]
            couvert = sum(vent.values())
            for k, w in vent.items():
                add(k, v * pa * w)
            if couvert < 0.999:
                add("non ventilé", v * pa * (1 - couvert))
        else:
            add(l.get(champ) or "non ventilé", v * pa)
        if pa < 1 and not actions_seulement:
            add("EUR" if dimension == "devises" else "hors actions", v * (1 - pa))
    return out


def hhi(valeurs: list) -> dict:
    tot = sum(valeurs)
    if tot <= 0:
        return {"hhi": None, "n_effectif": None}
    h = sum((v / tot) ** 2 for v in valeurs)
    return {"hhi": h, "n_effectif": 1 / h}


def top_n(montants: dict, n: int, denominateur: float) -> dict:
    tri = sorted(montants.items(), key=lambda kv: -kv[1])
    s = sum(v for _, v in tri[:n])
    return {"poids": s / denominateur, "lignes": [k for k, _ in tri[:n]]}


# ════════════════════════════════════════════════════════════════════════════
# 8. Risque
# ════════════════════════════════════════════════════════════════════════════

def rendements_simples(valeurs: list) -> list:
    return [b / a - 1 for a, b in zip(valeurs, valeurs[1:])]


def rendements_log(valeurs: list) -> list:
    return [math.log(b / a) for a, b in zip(valeurs, valeurs[1:])]


def volatilite_annualisee(rendements: list, periodes_par_an: int = 252, minimum: int = 60) -> dict:
    """Écart-type d'échantillon × √(périodes par an). Refusée sous `minimum` observations."""
    n = len(rendements)
    if n < minimum:
        return {"volatilite": None, "n": n, "motif": f"{n} observations < {minimum} requises"}
    return {"volatilite": statistics.stdev(rendements) * math.sqrt(periodes_par_an), "n": n}


def correlation(x: list, y: list) -> float:
    if len(x) != len(y) or len(x) < 3:
        raise ValueError("séries de même longueur ≥ 3 requises")
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return sxy / (sx * sy)


def intervalle_fisher(rho: float, n: int, z: float = 1.96) -> tuple:
    """Intervalle de confiance d'une corrélation (transformation de Fisher)."""
    if n <= 3:
        raise ValueError("n > 3 requis")
    zr = math.atanh(rho)
    se = 1 / math.sqrt(n - 3)
    return (math.tanh(zr - z * se), math.tanh(zr + z * se))


def sensibilite_change(lignes: list, expos_fonds: dict, choc_devise: dict) -> dict:
    """Impact d'une variation des devises contre l'euro (choc > 0 : la devise
    s'apprécie). Actifs en devise non couverts uniquement."""
    rep = repartition(lignes, expos_fonds, "devises", actions_seulement=False)
    impact = {k: rep.get(k, 0.0) * c for k, c in choc_devise.items()}
    return {"exposition": rep, "impact": impact, "total": sum(impact.values())}


def liquidites_mobilisables(ldds, compte_courant, matelas_cible, reserve_fiscale,
                            besoins_court_terme=0.0, inclure_compte_courant=False) -> dict:
    """Liquidités réellement déployables = disponible − matelas − impôt dû − besoins certains < 24 mois."""
    dispo = ldds + (compte_courant if inclure_compte_courant else 0.0)
    m = dispo - matelas_cible - reserve_fiscale - besoins_court_terme
    return {"disponible": dispo, "matelas": matelas_cible, "reserve_fiscale": reserve_fiscale,
            "besoins": besoins_court_terme, "mobilisable": max(0.0, m), "manque": min(0.0, m)}


# ════════════════════════════════════════════════════════════════════════════
# 9. Scénarios de stress et simulations
# ════════════════════════════════════════════════════════════════════════════

def _bornes(x, defaut=0.0):
    if x is None:
        return (defaut, defaut, False)
    if isinstance(x, (list, tuple)):
        return (float(x[0]), float(x[1]), True)
    return (float(x), float(x), False)


def choc_ligne(ligne: dict, scenario: dict, hyp: dict) -> tuple:
    """Rendement de la ligne sous le scénario : (bas, haut, incertain).
    Modèle explicite : part actions E, part tech T (IT + services de communication),
    part logiciels S (S ≤ T), bêta β, facteur crypto, superposition change."""
    t = ligne.get("type")
    if t in ("livret", "liquidite", "monetaire"):
        return (0.0, 0.0, False)
    if t == "formule":
        return (hyp.get("choc_formule_garantie", 0.0),) * 2 + (False,)
    if t == "crypto":
        s = scenario["chocs"].get("actions", 0.0) * hyp.get("facteur_crypto", 2.0)
        s = max(s, -0.95)
        return (s, s, False)
    e_lo, e_hi, e_inc = _bornes(ligne.get("part_actions", 1.0), 1.0)
    t_lo, t_hi, t_inc = _bornes(ligne.get("part_tech"), None) if ligne.get("part_tech") is not None else (0.0, hyp.get("part_tech_max_inconnue", 0.40), True)
    s_lo, s_hi, s_inc = _bornes(ligne.get("part_logiciel"), None) if ligne.get("part_logiciel") is not None else (0.0, hyp.get("part_logiciel_max_inconnue", 0.12), True)
    beta = float(ligne.get("beta", hyp.get("beta_defaut", 1.0)))
    ch = scenario["chocs"]
    res = []
    for e in (e_lo, e_hi):
        for tt in (t_lo, t_hi):
            for ss in (s_lo, min(s_hi, tt)):
                ss = min(ss, tt)
                if scenario.get("modele") == "global":
                    r_act = beta * ch["actions"]
                elif scenario.get("modele") == "tech":
                    r_act = tt * ch["tech"] + (1 - tt) * ch["reste"]
                elif scenario.get("modele") == "logiciel":
                    r_act = ss * ch["logiciel"] + (tt - ss) * ch["tech_hors_logiciel"] + (1 - tt) * ch["reste"]
                else:
                    raise ValueError(scenario.get("modele"))
                r_obl = ch.get("obligations", 0.0)
                r = e * r_act + (1 - e) * r_obl
                usd = float(ligne.get("part_usd", 0.0))
                if ch.get("usd"):
                    r = (1 + r) * (1 + usd * ch["usd"]) - 1
                res.append(r)
    return (min(res), max(res), e_inc or t_inc or s_inc)


def stress(lignes: list, scenario: dict, hyp: dict, denominateur: float) -> dict:
    """Impact déterministe d'un scénario : par ligne, par enveloppe, total, contributeurs."""
    par_ligne, par_env = [], {}
    tot_lo = tot_hi = 0.0
    for l in lignes:
        v = float(l.get("valeur_eur") or 0)
        lo, hi, inc = choc_ligne(l, scenario, hyp)
        i_lo, i_hi = v * lo, v * hi
        tot_lo += i_lo
        tot_hi += i_hi
        env = l.get("enveloppe", "?")
        a = par_env.setdefault(env, [0.0, 0.0])
        a[0] += i_lo
        a[1] += i_hi
        par_ligne.append({"id": l["id"], "enveloppe": env, "valeur": v, "impact_bas": i_lo,
                          "impact_haut": i_hi, "incertain": inc})
    contrib = sorted(par_ligne, key=lambda x: x["impact_bas"])[:5]
    return {"id": scenario["id"], "scenario": scenario["id"], "libelle": scenario.get("libelle"),
            "impact_min": tot_lo, "impact_max": tot_hi,
            "impact_pct_min": tot_lo / denominateur, "impact_pct_max": tot_hi / denominateur,
            "par_enveloppe": {k: {"min": v[0], "max": v[1]} for k, v in par_env.items()},
            "par_ligne": par_ligne, "principaux_contributeurs": [c["id"] for c in contrib],
            "fourchette": abs(tot_hi - tot_lo) > 0.005}


def regles_eligibles_sous_choc(dd_actuel: float, choc: float, tranches: list) -> list:
    """Tranches dont le seuil serait atteint si le marché baissait encore de `choc` :
    DD_nouveau = (1 + DD_actuel)(1 + choc) − 1."""
    dd_n = (1 + dd_actuel) * (1 + choc) - 1
    return [t for t in tranches if dd_n <= t["seuil"] + 1e-12], dd_n


def appliquer_operation(lignes: list, operation: dict) -> list:
    """Simule un arbitrage : retire `montant` de la ligne source, l'ajoute à la cible
    (créée si besoin, avec les caractéristiques fournies)."""
    out = copy.deepcopy(lignes)
    m = float(operation["montant"])
    src = next((l for l in out if l["id"] == operation["de"]), None) if operation.get("de") else None
    if src is not None:
        if src["valeur_eur"] + 1e-9 < m:
            raise ValueError(f"solde insuffisant sur {src['id']}")
        src["valeur_eur"] -= m
    dst = next((l for l in out if l["id"] == operation["vers"]), None)
    if dst is None:
        dst = dict(operation.get("nouvelle_ligne", {}), id=operation["vers"], valeur_eur=0.0)
        out.append(dst)
    dst["valeur_eur"] += m
    return out


def indicateurs_risque(lignes: list, hyp: dict, scenario_ref: dict, denominateur_hors_flux: float) -> dict:
    """Indicateurs comparables avant/après une opération."""
    actions = sum(float(l["valeur_eur"]) * _bornes(l.get("part_actions", 1.0), 1.0)[0]
                  for l in lignes if l.get("type") not in ("livret", "liquidite", "monetaire", "formule", "crypto"))
    st = stress(lignes, scenario_ref, hyp, denominateur_hors_flux)
    liq = sum(float(l["valeur_eur"]) for l in lignes if l.get("type") in ("livret", "liquidite") and l.get("disponible", True))
    vals = [float(l["valeur_eur"]) for l in lignes if l.get("type") == "action"]
    return {"part_actions": actions / denominateur_hors_flux, "perte_scenario_ref": st["impact_min"],
            "liquidites_disponibles": liq, "hhi_actions_directes": hhi(vals)["hhi"] if vals else None}


def simuler_decision(lignes, operation, hyp, scenario_ref, denominateur) -> dict:
    avant = indicateurs_risque(lignes, hyp, scenario_ref, denominateur)
    apres_l = appliquer_operation(lignes, operation)
    d2 = denominateur + (operation["montant"] if operation.get("financement_externe") else 0.0)
    apres = indicateurs_risque(apres_l, hyp, scenario_ref, d2)
    return {"operation": operation.get("libelle", operation["vers"]), "avant": avant, "apres": apres,
            "delta": {k: (apres[k] - avant[k]) if (apres[k] is not None and avant[k] is not None) else None
                      for k in avant}}


def monte_carlo(valeur_initiale, part_risquee, mu_annuel, sigma_annuel, mois=12, n=10000,
                nu=4, graine=20261123, rendement_sans_risque=0.0) -> dict:
    """Simulation probabiliste EXPÉRIMENTALE (loi de Student, ν = 4, mensuelle).
    Ce n'est pas une prévision : la distribution dépend entièrement des hypothèses."""
    rng = random.Random(graine)
    mu_m = mu_annuel / 12
    sig_m = sigma_annuel / math.sqrt(12)
    echelle = math.sqrt((nu - 2) / nu)
    finals, dd_sup20 = [], 0
    for _ in range(n):
        v, pic, pire = valeur_initiale, valeur_initiale, 0.0
        for _ in range(mois):
            z = rng.gauss(0, 1)
            chi2 = sum(rng.gauss(0, 1) ** 2 for _ in range(nu))
            t = z / math.sqrt(chi2 / nu)
            r = part_risquee * (mu_m + sig_m * echelle * t) + (1 - part_risquee) * rendement_sans_risque / 12
            v *= max(1 + r, 0.0)
            pic = max(pic, v)
            pire = min(pire, v / pic - 1)
        finals.append(v)
        if pire <= -0.20:
            dd_sup20 += 1
    finals.sort()

    def q(p):
        return finals[min(n - 1, int(p * n))]
    return {"avertissement": "simulation, pas une prévision", "n": n, "graine": graine,
            "p05": q(0.05), "p50": q(0.50), "p95": q(0.95),
            "proba_baisse_max_20pct": dd_sup20 / n,
            "hypotheses": {"mu": mu_annuel, "sigma": sigma_annuel, "nu": nu, "part_risquee": part_risquee}}


# ════════════════════════════════════════════════════════════════════════════
# 10. Fiscalité (règles 2026 vérifiées le 23/11/2026, à revérifier chaque année)
# ════════════════════════════════════════════════════════════════════════════

TAUX_DEFAUT = {"pfu_ir": 0.128, "ps": 0.186, "ps_assurance_vie": 0.172, "csg_deductible": 0.068,
               "verifie_le": "2026-11-23", "sources": ["impots.gouv.fr — plus-value mobilière (PFU 31,4 %)",
                                                      "impots.gouv.fr — brochure IR 2026, nouveautés (CSG déductible 6,8 %)"]}


def plus_value(quantite, prix_vente_eur, pru_eur, frais_eur=0.0) -> float:
    """Plus-value (ou moins-value) en EUR : prix de cession converti au jour de la
    cession, prix de revient moyen pondéré converti au jour de chaque achat."""
    return quantite * (prix_vente_eur - pru_eur) - frais_eur


def bilan_plus_values(cessions: list, mv_reportables: list, annee: int) -> dict:
    """Net de l'année (tous comptes-titres ordinaires du foyer confondus), puis
    imputation des moins-values reportables (10 ans, les plus anciennes d'abord)."""
    net = sum(float(c["plus_value"]) for c in cessions if lire_date(c["date"]).year == annee)
    report = sorted([dict(m) for m in mv_reportables if annee - 10 <= int(m["annee"]) < annee],
                    key=lambda m: int(m["annee"]))
    expirees = [m for m in mv_reportables if int(m["annee"]) < annee - 10]
    imputees, base = [], net
    if net > 0:
        for m in report:
            if base <= 0:
                break
            u = min(base, float(m["montant"]))
            base -= u
            m["montant"] = float(m["montant"]) - u
            imputees.append({"annee": m["annee"], "montant": u})
    nouvelle = -net if net < 0 else 0.0
    restantes = [m for m in report if m["montant"] > 1e-9]
    if nouvelle:
        restantes.append({"annee": annee, "montant": nouvelle})
    return {"net_annee": net, "imputations": imputees, "base_imposable": max(base, 0.0),
            "mv_reportables_restantes": restantes, "mv_expirees": expirees}


def impot_pfu(base, taux=TAUX_DEFAUT) -> dict:
    ir, ps = base * taux["pfu_ir"], base * taux["ps"]
    return {"ir": ir, "ps": ps, "total": ir + ps}


def impot_bareme_approx(base, tmi, taux=TAUX_DEFAUT) -> dict:
    """Option barème (globale pour tous les revenus de capitaux du foyer), en
    supposant que la base ne fait pas changer de tranche. La CSG déductible (6,8 %)
    réduit le revenu imposable de l'année suivante."""
    ir, ps = base * tmi, base * taux["ps"]
    eco = base * taux["csg_deductible"] * tmi
    return {"ir": ir, "ps": ps, "economie_csg_n1": eco, "total_net": ir + ps - eco,
            "hypothese": "pas de changement de tranche ; option globale ; à confirmer au simulateur officiel"}


def comparer_pfu_bareme(base, tmi, taux=TAUX_DEFAUT) -> dict:
    p, b = impot_pfu(base, taux), impot_bareme_approx(base, tmi, taux)
    return {"pfu": p["total"], "bareme": b["total_net"], "ecart": b["total_net"] - p["total"],
            "plus_favorable": "barème" if b["total_net"] < p["total"] else "PFU"}


def economie_recolte_moins_value(moins_value_latente, base_avant, taux_global) -> dict:
    """Économie d'impôt si une moins-value latente est réalisée avant le 31/12."""
    mv = abs(min(moins_value_latente, 0.0))
    utilisee = min(mv, max(base_avant, 0.0))
    return {"imputable_cette_annee": utilisee, "reportable": mv - utilisee,
            "economie": utilisee * taux_global}


def fiscalite_retrait_pea(retrait, valeur_pea, versements_cumules, taux=TAUX_DEFAUT) -> dict:
    """Retrait partiel d'un PEA de plus de 5 ans : prélèvements sociaux sur la
    fraction de gain contenue dans le retrait (taux 2026 : 18,6 %)."""
    gain_total = max(valeur_pea - versements_cumules, 0.0)
    part_gain = retrait * gain_total / valeur_pea if valeur_pea else 0.0
    return {"part_gain": part_gain, "ps": part_gain * taux["ps"]}


# ════════════════════════════════════════════════════════════════════════════
# 11. Notifications transactionnelles (boîte d'envoi)
# ════════════════════════════════════════════════════════════════════════════
# Cycle : EN_ATTENTE (persisté avant envoi) → ENVOYE (outil confirmé) → ACQUITTE
# (case cochée par l'utilisateur) → ARCHIVE ; ECHEC → nouvel essai ; ABANDONNE.

MAX_TENTATIVES = 3


def cle_evenement(regle, type_evt, seance, detail="") -> str:
    brut = f"{regle}|{type_evt}|{seance}|{detail}"
    return "E-" + hashlib.sha1(brut.encode("utf-8")).hexdigest()[:10]


def ajouter_evenement(etat: dict, evt: dict) -> tuple:
    """Idempotent : un même identifiant n'est jamais créé deux fois."""
    boite = etat.setdefault("evenements", [])
    for e in boite:
        if e["id"] == evt["id"]:
            return e, False
    e = dict(evt)
    e.setdefault("statut", "EN_ATTENTE")
    e.setdefault("tentatives", 0)
    e.setdefault("canaux", {})
    boite.append(e)
    return e, True


def evenements_a_envoyer(etat: dict) -> list:
    return [e for e in etat.get("evenements", [])
            if e.get("notifier") and e["statut"] in ("EN_ATTENTE", "ECHEC") and e["tentatives"] < MAX_TENTATIVES]


def confirmer_envoi(etat: dict, evt_id: str, canal: str, succes: bool, instant: str, detail="") -> dict:
    e = next((x for x in etat.get("evenements", []) if x["id"] == evt_id), None)
    if e is None:
        raise KeyError(evt_id)
    e["canaux"][canal] = {"succes": bool(succes), "instant": instant, "detail": detail}
    if canal == "message":
        e["tentatives"] += 1
    if any(c.get("succes") for c in e["canaux"].values()):
        if e["statut"] in ("EN_ATTENTE", "ECHEC"):
            e["statut"] = "ENVOYE"
            e["envoye_le"] = instant
    elif e["tentatives"] >= MAX_TENTATIVES:
        e["statut"] = "ABANDONNE"
    else:
        e["statut"] = "ECHEC"
    return e


def acquitter(etat: dict, ids: list, instant: str) -> int:
    n = 0
    for e in etat.get("evenements", []):
        if e["id"] in ids and e["statut"] == "ENVOYE":
            e["statut"], e["acquitte_le"] = "ACQUITTE", instant
            n += 1
    return n


def archiver(etat: dict, aujourd_hui, jours=30) -> int:
    j = lire_date(aujourd_hui)
    garde, arch = [], etat.setdefault("archives_evenements", [])
    for e in etat.get("evenements", []):
        age = (j - lire_date(e.get("seance") or j)).days
        if e["statut"] in ("ACQUITTE", "ABANDONNE") or (e["statut"] == "ENVOYE" and age > jours) or (
                not e.get("notifier") and age > jours):
            e2 = dict(e)
            e2["statut_final"] = e["statut"]
            e2["statut"] = "ARCHIVE"
            arch.append({k: e2[k] for k in ("id", "regle", "type", "seance", "statut_final")})
        else:
            garde.append(e)
    n = len(etat.get("evenements", [])) - len(garde)
    etat["evenements"] = garde
    etat["archives_evenements"] = arch[-200:]
    return n


# ════════════════════════════════════════════════════════════════════════════
# 12. Moteur de règles (machine à états)
# ════════════════════════════════════════════════════════════════════════════
# États : ARMEE, TOUCHEE_GELEE, ELIGIBLE, DECLENCHEE, CONSOMMEE, EN_ATTENTE,
#         EXPIREE, OBSERVATION, SUSPENDUE. « Non évaluée » n'est pas un état :
#         c'est le résultat d'une évaluation sans donnée suffisante (état inchangé).

LIBELLES_ETATS = {"ARMEE": "ARMÉE", "TOUCHEE_GELEE": "TOUCHÉE, GELÉE", "ELIGIBLE": "ÉLIGIBLE",
                  "DECLENCHEE": "DÉCLENCHÉE", "CONSOMMEE": "CONSOMMÉE", "EN_ATTENTE": "EN ATTENTE",
                  "EXPIREE": "EXPIRÉE", "OBSERVATION": "OBSERVATION", "SUSPENDUE": "SUSPENDUE",
                  "TOUCHE_GELE": "SEUIL TOUCHÉ, GELÉ", "AGGRAVATION": "AGGRAVATION",
                  "RESULTATS_PUBLIES": "RÉSULTATS PUBLIÉS", "RUPTURE_REGIME": "RUPTURE DE RÉGIME",
                  "DONNEE_CRITIQUE_MANQUANTE": "DONNÉE CRITIQUE MANQUANTE", "LIGNE_INTERDITE": "LIGNE INTERDITE",
                  "SORTIE_ELIGIBLE": "SORTIE D'ÉLIGIBILITÉ", "VEILLE": "VEILLE"}
EVENEMENTS_NOTIFIES = {"DECLENCHEE", "ELIGIBLE", "TOUCHE_GELE", "AGGRAVATION", "RESULTATS_PUBLIES",
                       "RUPTURE_REGIME", "DONNEE_CRITIQUE_MANQUANTE", "LIGNE_INTERDITE", "VEILLE"}


def _res(regle, etat, **kw):
    r = {"regle": regle, "etat": etat, "evenements": [], "verifications": [], "notes": [],
         "evaluee": True}
    r.update(kw)
    return r


def _non_evaluee(regle, st, motif, cle=None):
    r = _res(regle, st.get("etat", "ARMEE"), evaluee=False)
    r["notes"].append("NON ÉVALUÉE : " + motif)
    if cle:
        r["donnee_manquante"] = cle
    return r


def eval_seuil_poids(rid, p, st, ctx):
    """R1 — alerte de poids avec hystérésis et aggravation."""
    m = ctx["mesures"].get(p["mesure"])
    if not m or m.get("valeur") is None or m["qualite"] in ("MANQUANTE", "REJETEE", "DIVERGENTE"):
        return _non_evaluee(rid, st, f"mesure {p['mesure']} {m['qualite'] if m else 'MANQUANTE'}", p["mesure"])
    w, etat = m["valeur"], st.get("etat", "ARMEE")
    r = _res(rid, etat, valeur=w, seuil=p["seuil"], distance_pt=p["seuil"] - w)
    if etat == "ARMEE" and w >= p["seuil"] - 1e-12:
        if qualite_suffit_pour_transition(m):
            r["etat"] = "DECLENCHEE"
            r["evenements"].append({"type": "DECLENCHEE", "detail": f"poids {fmt_pct(w)}"})
            r["dernier_notifie"] = w
        else:
            r["verifications"].append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": "seuil franchi, qualité insuffisante"})
    elif etat == "DECLENCHEE":
        if w < p["rearmement"] - 1e-12:
            r["etat"] = "ARMEE"
            r["notes"].append(f"réarmée : {fmt_pct(w)} < {fmt_pct(p['rearmement'])}")
        elif w >= (st.get("dernier_notifie") or p["seuil"]) + p["aggravation"] - 1e-12:
            if qualite_suffit_pour_transition(m):
                r["evenements"].append({"type": "AGGRAVATION", "detail": f"poids {fmt_pct(w)}"})
                r["dernier_notifie"] = w
            else:
                r["verifications"].append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": "aggravation, qualité insuffisante"})
    elif w >= p["seuil"] - 0.002 and m["qualite"] != "VALIDE":
        r["verifications"].append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": "à moins de 0,2 pt du seuil"})
    veille = p.get("veille") or {}
    vj = ctx["mesures"].get(f"var_jour_{p['instrument']}")
    if vj and vj.get("valeur") is not None and vj["valeur"] <= veille.get("baisse_seance", -1) + 1e-12:
        if qualite_suffit_pour_transition(vj):
            r["evenements"].append({"type": "VEILLE", "detail": f"séance {fmt_pct(vj['valeur'], 1)} : rechercher la cause"})
        else:
            r["verifications"].append({"cle": [p["instrument"]], "raison": f"séance {fmt_pct(vj['valeur'], 1)} à confirmer"})
    act = ((ctx.get("faits") or {}).get(rid) or {}).get("actualite_importante")
    if act:
        r["evenements"].append({"type": "VEILLE", "detail": f"actualité : {str(act)[:60]}"})
    return r


def _garde_date_ok(p, ctx):
    g = p.get("garde_date")
    return g is None or lire_date(ctx["seance"]) >= lire_date(g)


def eval_tranches(rid, p, st, ctx):
    """R2, R3 — tranches sur drawdown, garde de date, rebond non verrouillé,
    détection de consommation par baisse du solde source."""
    cle_mesure = f"dd_{p['etf_retenu']}" if p.get("etf_retenu") else p["mesure"]   # V2 : ETF PEA retenu s'il est renseigné
    m = ctx["mesures"].get(cle_mesure)
    tr_st = {t["id"]: dict(t) for t in st.get("tranches", [])}
    if not m or m.get("valeur") is None or m["qualite"] in ("MANQUANTE", "REJETEE", "DIVERGENTE"):
        r = _non_evaluee(rid, st, f"mesure {cle_mesure} {m['qualite'] if m else 'MANQUANTE'}", cle_mesure)
        r["tranches"] = list(tr_st.values()) or [{"id": t["id"], "etat": "ARMEE"} for t in p["tranches"]]
        return r
    dd = m["valeur"]
    garde = _garde_date_ok(p, ctx)
    sortie = []
    evts, verifs, notes = [], [], []
    solde = ctx.get("soldes", {}).get(p.get("compte_source"))
    solde_prec = (ctx.get("soldes_precedents") or {}).get(p.get("compte_source"))
    baisse = (solde_prec - solde) if (solde is not None and solde_prec is not None) else None
    liq = ctx.get("liquidite") or {}
    a_sortir = 0.0          # tranches non encore consommées jusqu'à celle-ci incluse (encore sur le compte)
    plafond = p.get("plafond_matelas") and p.get("compte_source") == liq.get("compte_matelas")
    confs = ctx.get("confirmations") or {}
    restantes = [t for t in p["tranches"] if tr_st.get(t["id"], {}).get("etat") != "CONSOMMEE"
                 and confs.get(f"{rid}_{t['id']}") != "execute"]
    excedent = None
    if plafond and solde is not None and liq.get("matelas_cible") is not None:
        excedent = max(0.0, solde - float(liq["matelas_cible"]) - float(liq.get("reserve_fiscale") or 0.0))
    for t in p["tranches"]:
        s = tr_st.get(t["id"], {"id": t["id"], "etat": "ARMEE"})
        e0 = s.get("etat", "ARMEE")
        atteint = dd <= t["seuil"] + 1e-12
        ok_q = qualite_suffit_pour_transition(m)
        montant = float(t["montant"]) if isinstance(t["montant"], (int, float)) else None
        if e0 != "CONSOMMEE" and confs.get(f"{rid}_{t['id']}") != "execute" and montant is not None:
            if plafond:      # D1 : excédent au-delà du matelas et de la réserve, à parts égales, plafonné au montant V2
                montant = None if excedent is None else min(montant, excedent / max(1, len(restantes)))
                s["montant_effectif"] = montant
            if montant is not None:
                a_sortir += montant
        if solde is not None and liq.get("matelas_cible") is not None and montant is not None \
                and p.get("compte_source") == liq.get("compte_matelas") and e0 != "CONSOMMEE" \
                and confs.get(f"{rid}_{t['id']}") != "execute":
            apres = solde - a_sortir
            s["controle_liquidite"] = {"solde_avant": solde, "solde_apres": apres, "matelas": liq["matelas_cible"],
                                       "ecart_matelas": apres - liq["matelas_cible"],
                                       "tranches_comprises": a_sortir, "montant_tranche": montant,
                                       "note": "montant plafonné au matelas (D1)" if plafond else "information, ne bloque pas la règle"}
        if atteint and ok_q:
            s["seuil_atteint_une_fois"] = True
        conf = (ctx.get("confirmations") or {}).get(f"{rid}_{t['id']}")
        if conf == "execute" and e0 in ("ELIGIBLE", "TOUCHEE_GELEE", "ARMEE"):
            s["etat"] = "CONSOMMEE"
            notes.append(f"{t['id']} consommée (confirmation utilisateur)")
        elif e0 == "ARMEE" and atteint:
            if not ok_q:
                verifs.append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": f"{t['id']} : seuil atteint, qualité insuffisante"})
            elif garde:
                s["etat"] = "ELIGIBLE"
                evts.append({"type": "ELIGIBLE", "detail": t["id"]})
            else:
                s["etat"] = "TOUCHEE_GELEE"
                if not s.get("gel_notifie"):
                    evts.append({"type": "TOUCHE_GELE", "detail": t["id"]})
                    s["gel_notifie"] = True
        elif e0 == "TOUCHEE_GELEE":
            if not atteint and ok_q:
                s["etat"] = "ARMEE"
                notes.append(f"{t['id']} : seuil plus atteint, retour ARMÉE (tranche non verrouillée)")
            elif garde and atteint and ok_q:
                s["etat"] = "ELIGIBLE"
                evts.append({"type": "ELIGIBLE", "detail": t["id"]})
            elif not ok_q and (not atteint or garde):
                verifs.append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": f"{t['id']} : transition possible, qualité insuffisante"})
        elif e0 == "ELIGIBLE":
            if not atteint and not ok_q:
                verifs.append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": f"{t['id']} : rebond à confirmer avant exécution"})
            if not atteint and ok_q:
                s["etat"] = "ARMEE"
                evts.append({"type": "SORTIE_ELIGIBLE", "detail": t["id"]})
            elif baisse is not None and baisse >= (p["consommation_baisse_min"] if "consommation_baisse_min" in p else
                                                   p.get("consommation_ratio", 1.0) * float(s.get("montant_effectif") or t["montant"]
                                                   if isinstance(s.get("montant_effectif") or t["montant"], (int, float)) else float("inf"))):
                notes.append(f"{t['id']} probablement consommée (solde {p['compte_source']} −{fmt_eur(baisse, 0)}) : confirmation demandée")
                s["probablement_consommee"] = True
        if e0 == "ARMEE" and not atteint and dd <= t["seuil"] + 0.01 and m["qualite"] != "VALIDE":
            verifs.append({"cle": cles_a_verifier(m, [p["mesure"]]), "raison": f"{t['id']} : à moins de 1 pt du seuil"})
        s["distance_pt"] = dd - t["seuil"]
        sortie.append(s)
    clause = p.get("clause_fin_periode")
    clause_etat = st.get("clause")
    if clause and not clause_etat and lire_date(ctx["seance"]) > lire_date(clause["date_limite"]):
        if not any(x.get("seuil_atteint_une_fois") or x["etat"] in ("ELIGIBLE", "CONSOMMEE") for x in sortie):
            if str(clause.get("statut_validation", "")).startswith("VALIDE"):
                evts.append({"type": "ELIGIBLE", "detail": "clause de fin de période"})
                clause_etat = "ELIGIBLE"
            notes.append(f"fin de période sans baisse de {fmt_pct(-p['tranches'][0]['seuil'], 0)} : {clause['action']}")
        else:
            clause_etat = "SANS_OBJET"
    r = _res(rid, "/".join(x["etat"] for x in sortie), valeur=dd, tranches=sortie)
    r["evenements"], r["verifications"], r["notes"] = evts, verifs, notes
    if clause_etat:
        r["clause"] = clause_etat
    return r


def eval_resultats(rid, p, st, ctx):
    """R4, R6 — règles de publication (faits sourcés extraits par l'agent).

    R4 : critères connus à la publication ; cible fixée à la clôture de la séance
    de réaction (V2 : « aucun ordre avant la clôture du 2/12 »).
    R6 : alerte dès qu'un critère est rempli (V2 : « aucun ordre le 9/12 », rien de
    plus) ; la transition de CEO est surveillée avant comme après la publication.
    Une règle ÉLIGIBLE n'est jamais renotifiée (V2 §6)."""
    jour = lire_date(ctx["seance"])
    dpub = lire_date(p["date_publication"])
    etat = st.get("etat", "EN_ATTENTE")
    faits = (ctx.get("faits") or {}).get(rid, {})
    r = _res(rid, etat)
    if etat in ("EXPIREE", "CONSOMMEE"):
        return r
    if etat == "ELIGIBLE":
        r["cible"] = st.get("cible")
        r["notes"].append(f"éligible depuis le {fmt_date(st['depuis'], False) if st.get('depuis') else '?'} : en attente de ta décision")
        return r
    crit_brut = faits.get("criteres")          # None : non extraits ; {} : extraits, aucun rempli
    crit = crit_brut or {}
    if jour >= dpub:
        if faits.get("publie") and not st.get("publication_notifiee"):
            r["evenements"].append({"type": "RESULTATS_PUBLIES", "detail": p["ligne"]})
            r["publication_notifiee"] = True
        elif not faits.get("publie") and not st.get("publication_notifiee"):
            r["verifications"].append({"cle": [f"{rid}_publication"], "raison": "publication attendue non constatée"})
    if p["type"] == "resultats_vente":
        remplis = [c for c in p["criteres_vente"] if crit.get(c)]
        if remplis:
            r["etat"] = "ELIGIBLE"
            r["evenements"].append({"type": "ELIGIBLE", "detail": "vente : " + ", ".join(remplis)})
            r["action"] = p.get("action")
            if p.get("interdiction_ordre_le") and jour <= lire_date(p["interdiction_ordre_le"]):
                r["notes"].append(f"aucun ordre le {lire_date(p['interdiction_ordre_le']).strftime('%d/%m')} (V2)")
            return r
        r["etat"] = "EN_ATTENTE"
        if jour < dpub:
            r["notes"].append(f"publication le {fmt_date(dpub, False)} ; transition de CEO surveillée")
        elif (faits.get("publie") or st.get("publication_notifiee")) and crit.get("arr_guide_sous_10pct") is None:
            r["verifications"].append({"cle": [f"{rid}_criteres"], "raison": "croissance ARR guidée non extraite"})
        elif crit.get("arr_guide_sous_10pct") is False:
            r["notes"].append("croissance ARR guidée ≥ 10 % : critère non rempli ; transition de CEO toujours surveillée")
        return r
    dreac = lire_date(p.get("seance_reaction")) or seance_suivante(dpub)
    if jour < dpub:
        r["etat"] = "EN_ATTENTE"
        r["notes"].append(f"publication le {fmt_date(dpub, False)}")
        return r
    if jour < dreac:
        r["etat"] = "EN_ATTENTE"
        r["notes"].append(f"aucun ordre avant la clôture du {fmt_date(dreac, False)}")
        return r
    if crit_brut is None:
        r["verifications"].append({"cle": [f"{rid}_criteres"], "raison": "KPI non extraits"})
        return r
    decel = any(bool(crit.get(c)) for c in p.get("criteres_deceleration", []))
    var = faits.get("variation_reaction")
    if var is None:
        r["verifications"].append({"cle": [f"{rid}_variation_reaction"], "raison": "variation de la séance de réaction manquante"})
        return r
    rebond = var >= p["rebond_seuil"] - 1e-12
    cible = p["cible_si_les_deux"] if (decel and rebond) else p["cible_si_deceleration"] if decel else p["cible_si_rebond"] if rebond else None
    if cible is None:
        r["etat"] = "EXPIREE"
        r["notes"].append("ni décélération ni rebond ≥ seuil : conserver")
    else:
        r["etat"] = "ELIGIBLE"
        r["cible"] = cible
        r["evenements"].append({"type": "ELIGIBLE", "detail": f"alléger vers {fmt_pct(cible, 1)}"})
    return r


def eval_resultats_sans_regle(rid, p, st, ctx):
    """R5 — signaler la publication ; aucune action sans règle validée."""
    jour = lire_date(ctx["seance"])
    faits = (ctx.get("faits") or {}).get(rid, {})
    r = _res(rid, st.get("etat", "EN_ATTENTE"))
    if jour < lire_date(p["date_publication"]):
        r["notes"].append(f"publication le {fmt_date(p['date_publication'], False)} ; aucun ordre avant")
    if jour >= lire_date(p["date_publication"]) and faits.get("publie") and not st.get("publication_notifiee"):
        r["evenements"].append({"type": "RESULTATS_PUBLIES", "detail": p["ligne"]})
        r["publication_notifiee"] = True
        r["notes"].append("proposer une règle conditionnelle chiffrée (non validée, sans action)")
    return r


def eval_renfort(rid, p, st, ctx):
    """R7, R8 — renfort conditionnel jusqu'à un poids cible."""
    etat = st.get("etat", "ARMEE")
    if etat == "CONSOMMEE":
        return _res(rid, etat)
    if not _garde_date_ok(p, ctx):
        return _res(rid, "EN_ATTENTE", notes=[f"garde de date : {fmt_date(p['garde_date'], False)}"])
    conds = (ctx.get("faits") or {}).get(rid, {})
    m = ctx["mesures"].get(p["mesure_ratio"])
    if not m or m.get("valeur") is None or m["qualite"] in ("MANQUANTE", "REJETEE", "DIVERGENTE"):
        return _non_evaluee(rid, st, f"mesure {p['mesure_ratio']} indisponible", p["mesure_ratio"])
    ratio = m["valeur"]
    cond_prix = ratio <= p["ratio_max"] + 1e-12
    cond_fond = conds.get("conditions_fondamentales_ok")
    r = _res(rid, etat, valeur=ratio, seuil=p["ratio_max"], distance_pct=ratio / p["ratio_max"] - 1)
    if cond_prix and cond_fond is None:
        r["verifications"].append({"cle": [f"{rid}_fondamentaux"], "raison": "conditions fondamentales à vérifier"})
        return r
    if cond_prix and cond_fond and etat != "ELIGIBLE":
        if qualite_suffit_pour_transition(m):
            r["etat"] = "ELIGIBLE"
            r["evenements"].append({"type": "ELIGIBLE", "detail": f"renfort jusqu'à {fmt_pct(p['cible'], 1)}"})
        else:
            r["verifications"].append({"cle": cles_a_verifier(m, [p["mesure_ratio"]]), "raison": "condition de prix, qualité insuffisante"})
    elif etat == "ELIGIBLE" and not (cond_prix and cond_fond):
        r["etat"] = "ARMEE"
        r["notes"].append("conditions plus remplies")
    elif etat == "EN_ATTENTE":
        r["etat"] = "ARMEE"
    return r


def _condition_remplie(cond: dict, faits: dict, rid: str, verifs: list):
    """Une condition de critère : {"fait", "op" (vrai, faux, <, <=, >, >=), "valeur", "plage", "unite"}.
    Renvoie True, False, ou None si le fait est absent ou hors unité (alors vérification)."""
    v = faits.get(cond["fait"])
    op = cond.get("op", "vrai")
    if op == "vrai":
        return bool(v) if v is not None else None
    if op == "faux":
        return (not v) if v is not None else True       # « sans suspension » : absente = remplie
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        verifs.append({"cle": [f"{rid}_{cond['fait']}"], "raison": f"valeur illisible : {v}"})
        return None
    bas, haut = (cond.get("plage") or [None, None])
    if (bas is not None and x < bas) or (haut is not None and x > haut):
        verifs.append({"cle": [f"{rid}_{cond['fait']}"],
                       "raison": f"unité : {cond.get('unite', 'valeur hors plage')} attendue, reçu {v}"})
        return None
    s_ = cond["valeur"]
    return {"<": x < s_, "<=": x <= s_, ">": x > s_, ">=": x >= s_}[op]


def eval_sortie(rid, p, st, ctx):
    """Règle de sortie : vente totale (ÉLIGIBLE) si un critère est rempli ; revue sans vente
    sous un cours ; un critère peut être levé par un fait (« lever l'alerte », V2).
    Les critères sont décrits dans les paramètres : le moteur n'en connaît aucun."""
    etat = st.get("etat", "EN_ATTENTE")
    if etat in ("CONSOMMEE",):
        return _res(rid, etat)
    f = (ctx.get("faits") or {}).get(rid, {})
    r = _res(rid, etat)
    libelles = {c["code"]: c.get("libelle", c["code"]) for c in p["criteres_sortie"]}
    nouveaux = []
    for crit in p["criteres_sortie"]:
        if crit.get("leve_si") and f.get(crit["leve_si"]):
            continue
        resultats = [_condition_remplie(c, f, rid, r["verifications"]) for c in crit["conditions"]]
        if resultats and all(x is True for x in resultats):
            nouveaux.append(crit["code"])
    remplis = list(st.get("criteres_remplis") or [])
    remplis += [x for x in nouveaux if x not in remplis]
    for crit in p["criteres_sortie"]:
        if crit.get("leve_si") and f.get(crit["leve_si"]) and crit["code"] in remplis:
            remplis.remove(crit["code"])
            r["notes"].append(f"critère levé : {libelles[crit['code']]}")
    r["criteres_remplis"] = remplis
    if remplis and etat != "ELIGIBLE":
        r["etat"] = "ELIGIBLE"
        r["evenements"].append({"type": "ELIGIBLE", "detail": "vente totale : " + " ; ".join(libelles[x] for x in remplis)})
    elif not remplis and etat == "ELIGIBLE":
        r["etat"] = "EN_ATTENTE"
        r["evenements"].append({"type": "ALERTE_LEVEE", "detail": "plus aucun critère de sortie rempli"})
    elif not remplis:
        r["etat"] = "EN_ATTENTE"
    r["notes"].append("critères remplis : " + ", ".join(libelles[x] for x in remplis) if remplis
                      else "critères : " + ", ".join(libelles.values()) + " ; aucun rempli")
    m = ctx["mesures"].get(p.get("mesure_cours"))
    if m and m.get("valeur") is not None and m["valeur"] <= p["revue_cours_max"] + 1e-9:
        r["notes"].append(f"revue (re-notation grille v2) : clôture {fmt_nombre(m['valeur'], 2)} $ ≤ {p['revue_cours_max']} $")
        r["revue"] = True
    return r


def eval_plafonds(rid, p, st, ctx):
    """R10 — plafonds de poids, baisse de séance, publication de résultats.
    Un jour sans poids (Finary absent) conserve l'état de la ligne : rien n'est réémis."""
    r = _res(rid, st.get("etat", "ARMEE"), lignes={})
    prec_lignes = st.get("lignes") or {}
    declenchee = False
    pubs = (ctx.get("faits") or {}).get(rid, {}).get("resultats_publies") or {}
    for lig, q in p["lignes"].items():
        mw = ctx["mesures"].get(f"poids_{lig}")
        mv = ctx["mesures"].get(f"var_jour_{lig}")
        prec = dict(prec_lignes.get(lig) or {})
        if mw and mw.get("valeur") is not None:
            info = {"poids": mw["valeur"], "plafond": q["plafond"], "distance_pt": q["plafond"] - mw["valeur"],
                    "etat": "DECLENCHEE" if mw["valeur"] >= q["plafond"] - 1e-12 else "ARMEE"}
            if info["etat"] == "DECLENCHEE" and prec.get("etat") != "DECLENCHEE":
                r["evenements"].append({"type": "DECLENCHEE", "detail": f"{lig} plafond"})
        else:
            info = {k: v for k, v in prec.items() if k != "resultats_notifies"}
            info["non_evaluee"] = True
            r["notes"].append(f"{lig} : poids indisponible, état précédent conservé")
        if prec.get("resultats_notifies"):
            info["resultats_notifies"] = prec["resultats_notifies"]
        if info.get("etat") == "DECLENCHEE":
            declenchee = True
        if mv and mv.get("valeur") is not None and mv["valeur"] <= p["baisse_jour"] + 1e-12:
            if qualite_suffit_pour_transition(mv):
                r["evenements"].append({"type": "DECLENCHEE", "detail": f"{lig} séance {fmt_pct(mv['valeur'], 1)}"})
            else:
                r["verifications"].append({"cle": [lig], "raison": f"séance {fmt_pct(mv['valeur'], 1)} à confirmer (deux clôtures sourcées)"})
        if pubs.get(lig) and info.get("resultats_notifies") != str(pubs[lig]):
            r["evenements"].append({"type": "RESULTATS_PUBLIES", "detail": f"{lig} ({pubs[lig]})"})
            info["resultats_notifies"] = str(pubs[lig])
        r["lignes"][lig] = info
    r["etat"] = "DECLENCHEE" if declenchee else "ARMEE"
    return r


def eval_interdiction(rid, p, st, ctx):
    """R11 — alerte si une ligne interdite réapparaît dans un compte."""
    isins = {l.get("isin") for l in ctx.get("lignes", [])}
    r = _res(rid, "CONSOMMEE" if st.get("etat") in (None, "CONSOMMEE") else st["etat"])
    r["notes"].append("aucune ligne directe" if p["isin"] not in isins else "ligne directe détectée")
    if p["isin"] in isins:
        if st.get("etat") != "DECLENCHEE":
            r["evenements"].append({"type": "LIGNE_INTERDITE", "detail": p["libelle"]})
        r["etat"] = "DECLENCHEE"
    return r


def eval_rappels(rid, p, st, ctx):
    """R12, R13 — rappels datés et déclencheurs de révision (rapport)."""
    jour = lire_date(ctx["seance"])
    prec = lire_date(ctx.get("seance_precedente_officielle")) or (jour - UN_JOUR)
    r = _res(rid, st.get("etat", "EN_ATTENTE"))
    for d in p.get("rappels", []):
        if prec < lire_date(d) <= jour:
            r["notes"].append(f"rappel du {fmt_date(d, False)}")
    for ev in p.get("evenements", []):
        if prec < lire_date(ev["date"]) <= jour:
            r["notes"].append(f"événement : {ev['libelle']} ({fmt_date(ev['date'], False)})")
    for k, v in ((ctx.get("faits") or {}).get(rid, {}) or {}).items():
        if v:
            r["notes"].append(f"déclencheur de révision : {k}")
    futurs = sorted([(lire_date(d).isoformat(), f"rappel : {p.get('libelle', rid)}") for d in p.get("rappels", []) if lire_date(d) > jour] +
                    [(lire_date(e["date"]).isoformat(), e["libelle"]) for e in p.get("evenements", []) if lire_date(e["date"]) > jour])
    if futurs:                        # toujours, y compris un jour d'événement (« Ligne du jour »)
        r["prochain"] = {"date": futurs[0][0], "libelle": futurs[0][1]}
        r["notes"].append(f"prochain : {fmt_date(futurs[0][0], False)} ({futurs[0][1]})")
    return r


def eval_plafond_consolide(rid, p, st, ctx):
    """R14 (D7) — poids minimal consolidé par émetteur (direct + fonds), alerte au franchissement
    du seuil, réarmement sous le seuil de réarmement. Aucune vente automatique."""
    c = ctx.get("consolide")
    prec = st.get("emetteurs") or {}
    if not c or not c.get("emetteurs"):
        r = _non_evaluee(rid, st, "transparence indisponible (Finary ou expositions des fonds absents)", "expositions")
        r["emetteurs"] = prec
        return r
    d_exp = lire_date(c.get("date_expositions"))
    age = (lire_date(ctx["seance"]) - d_exp).days if d_exp else None
    if age is None or age > p.get("age_max_expositions_jours", 62):
        r = _non_evaluee(rid, st, f"expositions des fonds trop anciennes ({age if age is not None else '?'} jours)", "expositions")
        r["emetteurs"] = prec
        return r
    r = _res(rid, st.get("etat", "ARMEE"), emetteurs={})
    declenchee = False
    for em, v in sorted(c["emetteurs"].items(), key=lambda kv: -kv[1]["poids_min"]):
        w = v["poids_min"]
        e0 = (prec.get(em) or {}).get("etat", "ARMEE")
        if w >= p["seuil"] - 1e-12:
            etat_em = "DECLENCHEE"
            if e0 != "DECLENCHEE":
                r["evenements"].append({"type": "DECLENCHEE", "detail": f"{em} ≥ {fmt_pct(w)}"})
        elif e0 == "DECLENCHEE" and w >= p["rearmement"] - 1e-12:
            etat_em = "DECLENCHEE"                      # hystérésis : réarmée sous le seuil de réarmement
        else:
            etat_em = "ARMEE"
        if etat_em == "DECLENCHEE" or w >= p["seuil"] - 0.01:
            r["emetteurs"][em] = {"etat": etat_em, "poids_min": w}
        declenchee = declenchee or etat_em == "DECLENCHEE"
    top = max(c["emetteurs"].items(), key=lambda kv: kv[1]["poids_min"])
    r["valeur"], r["seuil"], r["premier"] = top[1]["poids_min"], p["seuil"], top[0]
    r["notes"].append(f"premier émetteur : {top[0]} ≥ {fmt_pct(top[1]['poids_min'])} (fiches des fonds du {fmt_date(d_exp, False)})")
    r["etat"] = "DECLENCHEE" if declenchee else "ARMEE"
    return r


EVALUATEURS = {"seuil_poids": eval_seuil_poids, "tranches_drawdown": eval_tranches, "plafond_consolide": eval_plafond_consolide,
               "resultats": eval_resultats, "resultats_vente": eval_resultats,
               "resultats_sans_regle": eval_resultats_sans_regle, "renfort": eval_renfort,
               "sortie": eval_sortie, "plafonds": eval_plafonds, "interdiction": eval_interdiction,
               "rappels": eval_rappels}


def evaluer_regles(params_regles: dict, etats: dict, ctx: dict) -> dict:
    out = {}
    for rid, p in params_regles.items():
        st = etats.get(rid, {}) or {}
        if st.get("etat") == "SUSPENDUE":
            out[rid] = _res(rid, "SUSPENDUE")
            continue
        f = EVALUATEURS[p["type"]]
        r = f(rid, p, st, ctx)
        if (ctx.get("confirmations") or {}).get(rid) == "execute" and p["type"] in (
                "resultats", "resultats_vente", "renfort", "sortie") and r["etat"] == "ELIGIBLE":
            r["etat"] = "CONSOMMEE"
            r["evenements"] = [e for e in r["evenements"] if e["type"] != "ELIGIBLE"]
            r["notes"].append("consommée (exécution confirmée par l'utilisateur)")
        r["mode_observation"] = p.get("statut_validation", "").startswith("PROVISOIRE")
        out[rid] = r
    return out


# ════════════════════════════════════════════════════════════════════════════
# 13. Régime de marché
# ════════════════════════════════════════════════════════════════════════════

def rupture_regime(courant: dict, precedent: dict | None, reference: dict | None, seuils: dict) -> dict:
    """Règle V2 (validée) : comparaison au DERNIER ÉTAT — 10 ans ±25 pb, Brent ±15 %,
    VIX franchissant 40 (à la hausse) ou 20 (à la baisse).
    Contrôle V3 (observation) : mêmes seuils contre l'état de RÉFÉRENCE ancré,
    pour détecter une dérive graduelle que la comparaison quotidienne ne voit pas."""
    def comparer(a, b):
        motifs = []
        if not a or not b:
            return motifs
        if a.get("us10y") is not None and b.get("us10y") is not None and abs(a["us10y"] - b["us10y"]) >= seuils["us10y_pb"] / 100 - 1e-9:
            motifs.append(f"10 ans US {fmt_nombre(b['us10y'], 2)} % → {fmt_nombre(a['us10y'], 2)} %")
        if a.get("brent") is not None and b.get("brent") is not None and abs(a["brent"] / b["brent"] - 1) >= seuils["brent_pct"] - 1e-9:
            motifs.append(f"Brent {fmt_nombre(b['brent'], 1)} $ → {fmt_nombre(a['brent'], 1)} $")
        if a.get("vix") is not None and b.get("vix") is not None:
            if b["vix"] <= seuils["vix_haut"] < a["vix"]:
                motifs.append(f"VIX franchit {seuils['vix_haut']} ({fmt_nombre(a['vix'], 1)})")
            if b["vix"] >= seuils["vix_bas"] > a["vix"]:
                motifs.append(f"VIX repasse sous {seuils['vix_bas']} ({fmt_nombre(a['vix'], 1)})")
        return motifs
    v2 = comparer(courant, precedent)
    v3 = comparer(courant, reference)
    return {"rupture_v2": bool(v2), "motifs_v2": v2, "derive_v3": bool(v3), "motifs_v3": v3}


# ════════════════════════════════════════════════════════════════════════════
# 14. État : structure, intégrité, sauvegardes, migration
# ════════════════════════════════════════════════════════════════════════════

CLES_ETAT = ["schema", "version_schema", "run_seq", "derniere_seance_officielle", "marche",
             "regime", "patrimoine", "regles", "evenements", "anomalies", "compteurs", "journal_runs"]


def etat_vide() -> dict:
    e = {"schema": SCHEMA_ETAT, "version_schema": VERSION_SCHEMA_ETAT, "run_seq": 0,
         "derniere_seance_officielle": None, "verrou": None, "marche": {}, "regime": {},
         "patrimoine": {}, "regles": {}, "evenements": [], "archives_evenements": [],
         "anomalies": [], "compteurs": {"manquant": {}}, "flux": [], "fiscalite": {},
         "journal_runs": []}
    e["empreinte"] = empreinte(e)
    return e


def sceller(etat: dict) -> dict:
    etat["empreinte"] = empreinte(etat)
    return etat


def verifier_etat(etat: dict) -> dict:
    erreurs = []
    if not isinstance(etat, dict):
        return {"ok": False, "erreurs": ["état non objet"]}
    for k in CLES_ETAT:
        if k not in etat:
            erreurs.append(f"clé manquante : {k}")
    if etat.get("schema") != SCHEMA_ETAT:
        erreurs.append("schéma inattendu")
    if str(etat.get("version_schema", "")).split(".")[0] != VERSION_SCHEMA_ETAT.split(".")[0]:
        erreurs.append("version de schéma incompatible")
    if "empreinte" in etat and etat["empreinte"] != empreinte(etat):
        erreurs.append("empreinte invalide : état modifié ou mal recopié")
    for e in etat.get("evenements", []):
        if e.get("statut") not in ("EN_ATTENTE", "ENVOYE", "ECHEC", "ACQUITTE", "ABANDONNE"):
            erreurs.append(f"événement {e.get('id')} : statut invalide")
    return {"ok": not erreurs, "erreurs": erreurs}


def rotation_sauvegardes(sauvegardes: list, etat_precedent: dict, n: int = 3) -> list:
    return ([copy.deepcopy(etat_precedent)] + list(sauvegardes or []))[:n]


def restaurer(etat_courant, sauvegardes: list) -> tuple:
    """Premier état valide : courant, puis sauvegardes N−1, N−2… ; sinon état vide."""
    for i, e in enumerate([etat_courant] + list(sauvegardes or [])):
        if e is not None and verifier_etat(e)["ok"]:
            return e, ("courant" if i == 0 else f"sauvegarde N−{i}")
    return etat_vide(), "état vide (mode sans état)"


def migrer_v2(x: dict) -> dict:
    """État V3 initial à partir des valeurs du journal V2 (saisies telles quelles)."""
    e = etat_vide()
    e.pop("empreinte", None)
    e["derniere_seance_officielle"] = x.get("seance_reference")
    e["origine"] = {"source": "journal V2", "lu_le": x.get("lu_le"), "revision_doc": x.get("revision_doc")}
    spx = x["spx"]
    e["marche"]["SPX"] = {
        "hwm_usd": {"valeur": spx["plus_haut_usd"], "date": spx["date_plus_haut"], "complet": False,
                    "qualite": spx.get("qualite_plus_haut", "PROBABLE"), "note": spx.get("note_plus_haut")},
        "hwm_eur": {"valeur": spx["plus_haut_usd"] / spx["fx_plus_haut"], "date": spx["date_plus_haut"],
                    "complet": False, "note": "initialisé au plus haut USD ; série EUR à reconstituer"},
        "clotures": [{"date": x["seance_reference"], "usd": spx["cloture"], "fx": spx["fx_jour"],
                      "eur": spx["cloture"] / spx["fx_jour"]}]}
    e["marche"]["EURUSD"] = {"fixings": {spx["date_plus_haut"]: spx["fx_plus_haut"], x["seance_reference"]: spx["fx_jour"]}}
    for k, v in (x.get("instruments") or {}).items():
        e["marche"][k] = v
    e["regime"] = {"reference": x.get("regime_reference"), "dernier": x.get("regime_dernier")}
    e["expositions"] = x.get("expositions", {})
    e["patrimoine"] = x.get("patrimoine", {})
    e["regles"] = x.get("regles", {})
    e["anomalies"] = [{"id": f"A-V2-{i+1:02d}", "code": "V2", "gravite": "MOYENNE", "texte": t,
                       "ouverte_le": x.get("lu_le", "")[:10], "origine": "V2"}
                      for i, t in enumerate(x.get("anomalies", []))]
    for ev in x.get("evenements_envoyes", []):
        ev = dict(ev, id=cle_evenement(ev["regle"], ev["type"], ev["seance"], ev.get("detail", "")))
        ajouter_evenement(e, dict(ev, statut="ENVOYE", tentatives=1, notifier=True,
                                  canaux={"message": {"succes": True, "instant": ev.get("instant"), "detail": "repris de la V2 (non vérifiable)"}}))
    e["journal_runs"].append({"run_id": "migration-v2", "instant": x.get("lu_le"), "mode": "MIGRATION"})
    return sceller(e)


# ════════════════════════════════════════════════════════════════════════════
# 15. Pipeline d'exécution
# ════════════════════════════════════════════════════════════════════════════

def _alias(nom, type_compte, params):
    for regle in params.get("alias_comptes", []):
        if regle.get("nom") == nom:
            return regle["alias"]
        if regle.get("prefixe") and str(nom).startswith(regle["prefixe"]):
            return regle["alias"]
        if regle.get("type") and regle.get("type") == type_compte and not regle.get("nom") and not regle.get("prefixe"):
            return regle["alias"]
    return None


def normaliser_finary(fin_brut: dict, params: dict) -> dict:
    """Relevé compact recopié par l'agent → comptes avec alias, lignes avec
    identifiant (par ISIN) et caractéristiques ; les comptes sans ligne
    (livret, compte courant, crypto) deviennent une ligne synthétique."""
    if not fin_brut:
        return {}
    fin = copy.deepcopy(fin_brut)
    par_isin = {v["isin"]: k for k, v in params.get("lignes", {}).items() if v.get("isin")}
    alias_par_nom = {}
    for c in fin.get("comptes", []):
        c["alias"] = c.get("alias") or _alias(c.get("nom"), c.get("type"), params) or c.get("nom")
        c["dans_fa"] = c.get("type") not in ("checking", "credit", "loan")
        alias_par_nom[c.get("nom")] = c["alias"]
    lignes = []
    for l in fin.get("lignes", []):
        l = dict(l)
        l["compte"] = alias_par_nom.get(l.get("compte"), l.get("compte"))
        lid = par_isin.get(l.get("isin")) if l.get("isin") else None
        if lid is None:
            lid = l.get("isin") or f"LIQ_{l['compte']}"
        elif not params["lignes"][lid].get("compte") or params["lignes"][lid]["compte"] != l["compte"]:
            lid = f"{lid}@{l['compte']}"   # fonds présent dans plusieurs enveloppes
        base = params.get("lignes", {}).get(lid.split("@")[0], {})
        car = {k: v for k, v in base.items() if k not in ("isin", "compte")}
        if not l.get("isin"):
            car.setdefault("type", "liquidite")
        car.setdefault("disponibilite", (params.get("disponibilite_par_compte") or {}).get(l["compte"], "inconnue"))
        l = {**car, **l, "id": lid, "enveloppe": l["compte"]}
        lignes.append(l)
    comptes_avec_lignes = {l["compte"] for l in lignes}
    for c in fin.get("comptes", []):
        if c["alias"] not in comptes_avec_lignes:
            t = params.get("comptes_sans_lignes", {}).get(c["alias"], {"type": "liquidite"})
            lignes.append({**t, "id": c["alias"], "compte": c["alias"], "enveloppe": c["alias"],
                           "valeur_eur": float(c["valeur"]), "synthetique": True,
                           "dans_fa": c["dans_fa"]})
    fin["lignes"] = lignes
    return fin


def appliquer_initialisations(params, etat_marche, collecte, seance) -> list:
    """Valeurs de référence (plus hauts, clôtures de référence) : enregistrées une
    fois, avec leur source, seulement si la donnée est VALIDE ou PROBABLE primaire."""
    journal = []
    for ini in params.get("initialisations", []):
        cible = etat_marche.setdefault(ini["instrument"], {})
        champ = ini["champ"]
        if (cible.get(champ) or {}).get("init_verifie"):
            continue
        dc = ini.get("date_cible") or seance
        v = valider_observations(collecte.get("observations", []), ini["cle"], dc, ini.get("nature", "action"),
                                 anteriorite_max_jours=ini.get("anteriorite_max_jours", 3))
        if v.get("valeur") is None:
            continue
        if qualite_suffit_pour_transition(v):
            obs = next(o for o in collecte["observations"] if o["cle"] == ini["cle"])
            ancien = cible.get(champ) or {}
            cible[champ] = {"valeur": v["valeur"], "date": obs.get("date_valeur") or dc, "init_verifie": True,
                            "qualite": v["qualite"], "sources": v["sources"], "complet": ini.get("complet", False)}
            if ancien.get("valeur") and abs(ancien["valeur"] / v["valeur"] - 1) > 0.001:
                journal.append(f"{ini['instrument']}.{champ} : {ancien['valeur']} remplacé par {v['valeur']} (vérifié)")
            else:
                journal.append(f"{ini['instrument']}.{champ} initialisé à {v['valeur']} ({v['qualite']})")
        else:
            journal.append(f"{ini['instrument']}.{champ} : observation {v['qualite']}, non retenue")
    return journal


def controler_finary(fin: dict, etat_prec: dict, params: dict, maintenant: dt.datetime) -> list:
    """Cohérence interne et fraîcheur du relevé Finary (sans le corriger)."""
    an = []
    ctl = params.get("controles", {})
    tol = ctl.get("ecart_lignes_compte_max", 0.005)
    erreurs, anciennes = {}, {}
    for c in fin.get("comptes", []):
        lignes = [l for l in fin.get("lignes", []) if l.get("compte") == c["alias"] and not l.get("synthetique")]
        if lignes:
            s = sum(float(l["valeur_eur"]) for l in lignes)
            if c.get("valeur") and abs(s / float(c["valeur"]) - 1) > tol:
                an.append({"code": "SOMME_LIGNES", "gravite": "HAUTE",
                           "texte": f"{c['alias']} : somme des lignes {fmt_eur(s)} ≠ solde {fmt_eur(float(c['valeur']))} (recopie à vérifier)"})
        dsync = c.get("derniere_synchro_reussie")
        if dsync:
            age_h = (maintenant - lire_instant(dsync)).total_seconds() / 3600
            seuil_h = (ctl.get("fraicheur_max_heures_par_compte") or {}).get(c["alias"], ctl.get("fraicheur_max_heures", 30))
            if age_h > seuil_h:
                anciennes.setdefault(f"{age_h:.0f}", []).append(c["alias"])
        if c.get("synchro_ok") is False:
            # La synchro « réussie » de l'établissement ne dit pas quels comptes elle a rafraîchis : la date
            # de mise à jour du compte est donnée à côté (la V2 la retient : « CTO/PEA/PER/LDDS du 20/11 18h31 UTC »).
            maj = c.get("maj_compte")
            try:
                maj_txt = lire_instant(maj).astimezone(dt.timezone.utc).strftime("%d/%m %H:%M UTC") if maj else None
            except ValueError:
                maj_txt = str(maj)
            erreurs.setdefault((c.get("etat_synchro", "erreur"), dsync), []).append(
                c["alias"] + (f" (compte mis à jour le {maj_txt})" if maj_txt else ""))
    for (etat_s, dsync), comptes in erreurs.items():
        an.append({"code": "SYNCHRO_ERREUR", "gravite": "MOYENNE",
                   "texte": f"synchronisation en erreur ({etat_s}) pour {', '.join(comptes)} ; dernière synchro réussie {dsync}"})
    for age, comptes in anciennes.items():
        an.append({"code": "SYNCHRO_ANCIENNE", "gravite": "MOYENNE",
                   "texte": f"dernière synchro réussie il y a {age} h : {', '.join(comptes)}"})
    fa0 = (etat_prec.get("patrimoine") or {}).get("fa_total")
    fa1 = fin.get("fa_total")
    if fa0 and fa1 and abs(fa1 / fa0 - 1) > params.get("controles", {}).get("saut_denominateur_max", 0.03):
        an.append({"code": "SAUT_DENOMINATEUR", "gravite": "HAUTE",
                   "texte": f"actifs financiers {fmt_eur(fa0)} → {fmt_eur(fa1)} ({'+' if fa1 >= fa0 else ''}{fmt_pct(fa1 / fa0 - 1)}) : flux ou erreur de synchro ?"})
    return an


def _obs_valide(collecte, cle, seance, nature, precedent=None, anteriorite=0, exigences=None):
    return valider_observations(collecte.get("observations", []), cle, seance, nature, precedent, anteriorite, exigences)


def _derniere_cloture(etat, cle):
    pts = ((etat.get("marche") or {}).get(cle) or {}).get("clotures") or []
    return pts[-1] if pts else None


def _valeur_cloture(x):
    return x.get("v", x.get("usd"))


def _cloture_a(clotures, d):
    """Clôture enregistrée à la date d (ou None)."""
    k = lire_date(d).isoformat()
    for x in reversed(clotures or []):
        if x.get("date") == k and _valeur_cloture(x) is not None:
            return float(_valeur_cloture(x))
    return None


def _ajouter_cloture(clotures, d, valeur, **extra):
    k = lire_date(d).isoformat()
    for x in clotures:
        if x.get("date") == k:
            x.update({"v": valeur, **extra})
            return
    clotures.append({"date": k, "v": valeur, **extra})
    clotures.sort(key=lambda x: x["date"])


def seances_par_marche(seance, maintenant=None) -> dict:
    """Séance cible de chaque marché : NYSE = séance du run ; Euronext = dernière
    séance parisienne clôturée à l'instant du run (elle peut différer, ex. Thanksgiving)."""
    out = {"XNYS": lire_date(seance).isoformat()}
    if maintenant is not None:
        try:
            out["XPAR"] = derniere_seance_cloturee(maintenant, "XPAR").isoformat()
        except RuntimeError:
            pass
    out.setdefault("XPAR", out["XNYS"])
    return out


def calculer_mesures(params, etat, collecte, seance, maintenant=None) -> tuple:
    """Mesures dérivées à partir des observations validées et du relevé Finary.

    Clôtures : conservées pour chaque instrument (10 dernières). En rattrapage, les
    clôtures des séances manquées, collectées avec leur date, sont intégrées dans
    l'ordre (plus hauts, fenêtre 52 semaines, variations) ; les règles ne sont
    évaluées que sur la séance cible. Seule une clôture suffisante pour une
    transition (qualite_suffit_pour_transition) peut faire monter un plus haut."""
    mes, nouveaux_marche, valid = {}, copy.deepcopy(etat.get("marche") or {}), {}
    fin = collecte.get("finary_normalise") or normaliser_finary(collecte.get("finary", {}), params)
    fa = fin.get("fa_total")
    lignes = {l["id"]: l for l in fin.get("lignes", [])}
    instr = params.get("instruments", {})
    obs = collecte.get("observations", [])
    seances = seances_par_marche(seance, maintenant)
    mes["_initialisations"] = appliquer_initialisations(params, nouveaux_marche, collecte, seance)
    # Fixings BCE : chaque taux collecté est rangé à sa propre date de publication
    fixings = nouveaux_marche.setdefault("EURUSD", {}).setdefault("fixings", {})
    for k, v in ((params.get("fixings_initiaux") or {}).items()):
        fixings.setdefault(k, v)
    for d_obs in sorted({o.get("date_obs") for o in obs if o.get("cle") == "EURUSD" and o.get("date_obs")}):
        vf = valider_observations(obs, "EURUSD", d_obs, "change", exigences=instr.get("EURUSD"))
        if vf.get("valeur") is not None and vf["qualite"] in ("VALIDE", "PROBABLE"):
            fixings[d_obs] = vf["valeur"]
    # Validation de chaque donnée attendue, séances manquées comprises
    nouvelles, integrees = {}, {}
    for cle, d in instr.items():
        marche = d.get("marche", "XNYS")
        dc = lire_date(seances.get(marche, seance))
        nature = d.get("nature", "action")
        if cle == "EURUSD":
            valid[cle] = valider_observations(obs, cle, dc, nature, None, d.get("anteriorite_max_jours", 0), d)
            valid[cle]["date_seance"] = dc.isoformat()
            continue
        m = nouveaux_marche.setdefault(cle, {})
        cl = m.setdefault("clotures", [])
        ajout = []
        derniere = lire_date(cl[-1]["date"]) if cl else None
        if derniere and derniere < dc:
            for x in [s for s in seances_entre(derniere, dc, marche) if s < dc][-5:]:
                vx = valider_observations(obs, cle, x, nature, _cloture_a(cl, seance_precedente(x, marche)), 0, d)
                if vx.get("valeur") is not None and vx["qualite"] in ("VALIDE", "PROBABLE"):
                    _ajouter_cloture(cl, x, vx["valeur"], q=vx["qualite"], ok=qualite_suffit_pour_transition(vx))
                    ajout.append((x.isoformat(), vx["valeur"], vx))
                    integrees.setdefault(cle, []).append(x.isoformat())
        prec = _cloture_a(cl, seance_precedente(dc, marche))
        v = valider_observations(obs, cle, dc, nature, prec, d.get("anteriorite_max_jours", 0), d)
        v["date_seance"] = dc.isoformat()
        if prec is None and v.get("valeur") is not None:
            v["drapeaux"].append("clôture de la séance précédente inconnue : variation de séance non calculée")
        if v.get("valeur") is not None and v["qualite"] in ("VALIDE", "PROBABLE") and v.get("date_retenue") == dc.isoformat():
            _ajouter_cloture(cl, dc, v["valeur"], q=v["qualite"], ok=qualite_suffit_pour_transition(v))
            ajout.append((dc.isoformat(), v["valeur"], v))
        m["clotures"] = cl[-10:]
        valid[cle] = v
        nouvelles[cle] = ajout
    mes["_seances_integrees"] = integrees
    fx = valid.get("EURUSD", {})
    # S&P 500 : plus hauts USD et EUR (incrémentaux), formule V2 (fait foi pour R2), mesure exacte (observation)
    spx = valid.get("SPX", {})
    m = nouveaux_marche.setdefault("SPX", {})
    ph = m.get("plus_haut_verifie")
    if ph and ph.get("init_verifie") and ph.get("valeur"):
        hu = m.get("hwm_usd") or {}
        if not hu.get("valeur") or ph["valeur"] > hu["valeur"] + 1e-9 or not hu.get("init_verifie"):
            m["hwm_usd"] = {"valeur": ph["valeur"], "date": ph["date"], "complet": True, "init_verifie": True}
            fxp, _ = fixing_a_date(fixings, lire_date(ph["date"]))
            he = m.get("hwm_eur") or {}
            if fxp and (not he.get("valeur") or ph["valeur"] / fxp > he["valeur"]):
                m["hwm_eur"] = {"valeur": ph["valeur"] / fxp, "date": ph["date"], "complet": False,
                                "note": "plus haut EUR ≥ valeur EUR du plus haut USD"}
    for x, vx, qx in nouvelles.get("SPX", []):
        fx_x, _ = fixing_a_date(fixings, lire_date(x))
        if fx_x:
            for c in m.get("clotures", []):
                if c["date"] == x:
                    c.update({"fx": fx_x, "eur": vx / fx_x})
        if not qualite_suffit_pour_transition(qx):
            continue
        m["hwm_usd"] = maj_plus_haut(m.get("hwm_usd"), x, vx)
        if fx_x:
            m["hwm_eur"] = maj_plus_haut(m.get("hwm_eur"), x, vx / fx_x)
    if spx.get("valeur") is not None and fx.get("valeur") is not None and m.get("hwm_usd"):
        hwm_usd, hwm_eur = m["hwm_usd"], m.get("hwm_eur") or {}
        eurv = spx["valeur"] / fx["valeur"]
        q = qualite_combinee(spx, fx)
        src = spx.get("sources", []) + fx.get("sources", [])
        mes["dd_usd_spx"] = {"valeur": drawdown(spx["valeur"], max(hwm_usd["valeur"], spx["valeur"])),
                             "qualite": q, "sources": src}
        fx_hwm, d_fx_hwm = fixing_a_date(fixings, lire_date(hwm_usd["date"]))
        comp_ph = {"cle": "SPX_PLUS_HAUT", "qualite": "VALIDE" if hwm_usd.get("init_verifie") else (hwm_usd.get("qualite") or "DOUTEUSE"),
                   "sources": [{"type": "primaire", "extraction": "extrait_textuel"}] if hwm_usd.get("init_verifie") else []}
        if fx_hwm:
            mes["dd_eur_spx_formule_v2"] = {
                "valeur": drawdown_eur_formule_v2(spx["valeur"], max(hwm_usd["valeur"], spx["valeur"]), fx_hwm, fx["valeur"]),
                "qualite": qualite_combinee(spx, fx, comp_ph), "sources": src,
                "cles_source": ["SPX", "EURUSD"] + ([] if hwm_usd.get("init_verifie") else ["SPX_PLUS_HAUT"]),
                "composantes": [spx, fx, comp_ph],
                "plus_haut_usd": hwm_usd, "fx_plus_haut": fx_hwm, "date_fx_plus_haut": d_fx_hwm.isoformat(),
                "mesure": "formule V2"}
        else:
            mes["dd_eur_spx_formule_v2"] = {"valeur": None, "qualite": "MANQUANTE",
                                            "note": f"fixing BCE du {hwm_usd['date']} (date du plus haut) inconnu"}
        if hwm_eur.get("valeur"):
            mes["dd_eur_spx"] = {"valeur": drawdown(eurv, max(hwm_eur["valeur"], eurv)), "qualite": qualite_combinee(spx, fx, comp_ph),
                                 "sources": src,
                                 "borne": "majorant, plus haut EUR incomplet" if not hwm_eur.get("complet") else "exact",
                                 "cles_source": ["SPX", "EURUSD"] + ([] if hwm_usd.get("init_verifie") else ["SPX_PLUS_HAUT"]),
                                 "composantes": [spx, fx, comp_ph],
                                 "plus_haut_eur": hwm_eur, "plus_haut_usd": hwm_usd, "valeur_eur": eurv,
                                 "mesure": "baisse exacte en EUR"}
    # Drawdown des instruments suivis en plus haut historique (paramètre suivi_plus_haut) : mesure dd_<clé>
    for cle, d in instr.items():
        if not d.get("suivi_plus_haut"):
            continue
        m = nouveaux_marche.setdefault(cle, {})
        for x, vx, qx in nouvelles.get(cle, []):
            if qualite_suffit_pour_transition(qx) and (m.get("hwm") or {}).get("init_verifie"):
                m["hwm"] = maj_plus_haut(m.get("hwm"), x, vx)
        c, h = valid.get(cle, {}), m.get("hwm") or {}
        if c.get("valeur") is not None and h.get("init_verifie"):
            mes[f"dd_{cle}"] = {"valeur": drawdown(c["valeur"], max(h["valeur"], c["valeur"])), "qualite": c["qualite"],
                                "sources": c["sources"], "mesure": d.get("libelle_mesure", f"drawdown de {cle}"),
                                "cles_source": [cle], "composantes": [c], "plus_haut": h}
        else:
            mes[f"dd_{cle}"] = {"valeur": None, "qualite": "MANQUANTE",
                                "note": f"plus haut de {cle} non initialisé sur une source vérifiée" if not h.get("init_verifie")
                                else f"clôture de {cle} manquante"}
    # Poids recalculé de la ligne surveillée par R1, prix seuil exact, dérive depuis le dernier état
    for rid, rp in params.get("regles", {}).items():
        if rp["type"] == "seuil_poids":
            lig = lignes.get(rp["ligne"])
            cours = valid.get(rp["instrument"], {})
            if lig and fa and cours.get("valeur") is not None and fx.get("valeur") is not None:
                pr = poids_recalcule(float(lig["quantite"]), cours["valeur"], fx["valeur"], float(lig["valeur_eur"]), fa)
                mm = {"valeur": pr["poids"], "qualite": qualite_combinee(cours, fx),
                      "sources": cours.get("sources", []) + fx.get("sources", []),
                      "detail": pr, "cles_source": [rp["instrument"], "EURUSD"],
                      "composantes": [cours, fx], "ligne": rp["ligne"]}
                reste = pr["denominateur_recalcule"] - pr["valeur_recalculee"]
                mm["prix_seuil_eur"] = prix_seuil(float(lig["quantite"]), rp["seuil"], reste)
                mm["prix_seuil_usd"] = mm["prix_seuil_eur"] * fx["valeur"]
                mm["hausse_requise"] = mm["prix_seuil_eur"] / pr["cours_eur"] - 1
                suivi = ((etat.get("regles") or {}).get(rid) or {}).get("suivi") or {}
                if suivi.get("poids") and suivi.get("denominateur") and suivi.get("seance") != lire_date(seance).isoformat():
                    v0 = suivi["poids"] * suivi["denominateur"]
                    der = decomposer_derive_poids(v0, suivi["denominateur"] - v0, pr["valeur_recalculee"], reste)
                    der["depuis"] = suivi["seance"]
                    der["cause"] = ("reste du patrimoine" if abs(der["effet_reste"]) > abs(der["effet_ligne"]) else "ligne")
                    mm["derive"] = der
                mes[rp["mesure"]] = mm
    # Poids Finary de chaque ligne, variations de séance
    if fa:
        for lid, l in lignes.items():
            mes[f"poids_{lid}"] = {"valeur": float(l["valeur_eur"]) / fa, "qualite": "PROBABLE",
                                   "sources": [{"nom": "Finary", "type": "finary"}]}
    for cle, v in valid.items():
        if v.get("variation") is not None:
            mes[f"var_jour_{cle}"] = {"valeur": v["variation"], "qualite": v["qualite"], "sources": v["sources"],
                                      "composantes": [v], "date": v.get("date_seance")}
    # Ratios de prix pour les renforts (R7, R8) et cours de revue (R9)
    for rid, rp in params.get("regles", {}).items():
        if rp["type"] == "renfort":
            c = valid.get(rp["instrument"], {})
            m = nouveaux_marche.setdefault(rp["instrument"], {})
            if rp.get("reference") == "plus_haut_52s":
                fen = m.get("fenetre_max")
                init = m.get("plus_haut_init")
                if fen is None and init and init.get("valeur") and init.get("date") and init.get("init_verifie"):
                    fen = [{"date": init["date"], "v": init["valeur"]}]
                if fen is not None:
                    for x, vx, qx in nouvelles.get(rp["instrument"], []):
                        if qualite_suffit_pour_transition(qx):
                            fen = maj_fenetre_max(fen, x, vx)
                m["fenetre_max"] = fen
            if c.get("valeur") is not None:
                if rp.get("reference") == "cloture_fixe":
                    ref = (m.get("reference") or {}).get("valeur")
                    if ref:
                        mes[rp["mesure_ratio"]] = {"valeur": c["valeur"] / ref, "qualite": c["qualite"],
                                                   "sources": c["sources"], "cles_source": [rp["instrument"]],
                                                   "composantes": [c]}
                    else:
                        mes[rp["mesure_ratio"]] = {"valeur": None, "qualite": "MANQUANTE", "note": "clôture de référence non relevée"}
                elif rp.get("reference") == "plus_haut_52s":
                    fen = m.get("fenetre_max")
                    if fen:
                        ph = (max(fen[0]["v"], c["valeur"]), fen[0]["date"])
                        mes[rp["mesure_ratio"]] = {"valeur": c["valeur"] / ph[0], "qualite": c["qualite"],
                                                   "sources": c["sources"], "plus_haut": ph,
                                                   "cles_source": [rp["instrument"]], "composantes": [c]}
        if rp["type"] == "sortie" and rp.get("mesure_cours"):
            c = valid.get(rp["instrument"], {})
            if c.get("valeur") is not None:
                mes[rp["mesure_cours"]] = {"valeur": c["valeur"], "qualite": c["qualite"], "sources": c["sources"]}
    return mes, nouveaux_marche, valid


def _montant_operation(op, lignes_par_id, fa):
    m = op["montant"]
    if isinstance(m, (int, float)):
        return float(m)
    lid, cible = op.get("ligne_poids"), op.get("poids_cible")
    v = float(lignes_par_id[lid]["valeur_eur"]) if lid in lignes_par_id else 0.0
    if m == "vers_poids_cible":
        if op.get("financement_externe"):
            return max(0.0, (cible * fa - v) / (1 - cible))
        return max(0.0, cible * fa - v)
    if m == "allegement_poids_cible":
        return max(0.0, v - cible * fa)
    if m == "reste" and lid in lignes_par_id:
        return v
    raise ValueError(f"montant inconnu : {m}")


def poids_consolides(fin: dict, expos: dict) -> dict | None:
    """Poids minimal consolidé de chaque émetteur (direct + fonds), sur le dénominateur Finary.
    Date de référence : la plus ancienne des fiches de fonds qui détaillent des émetteurs."""
    if not fin or not fin.get("fa_total") or not fin.get("lignes") or not expos:
        return None
    lignes = [l for l in fin["lignes"] if l.get("dans_fa", True)]
    ex = expositions_emetteurs(lignes, expos)
    dates = [lire_date(v.get("date")) for v in expos.values()
             if isinstance(v, dict) and v.get("emetteurs") and _date_iso(v.get("date"))]
    d = float(fin["fa_total"])
    return {"denominateur": d, "date_expositions": min(dates).isoformat() if dates else None,
            "emetteurs": {k: {"total_min": v["total_min"], "poids_min": v["total_min"] / d, "direct": v["direct"]}
                          for k, v in ex["emetteurs"].items()}}


def _date_iso(x) -> bool:
    return isinstance(x, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", x))


def diagnostic_complet(fin: dict, params: dict, etat: dict, mesures: dict) -> dict:
    """Moteurs B, C, D (hebdomadaire) : allocation, transparence, concentration,
    liquidité, scénarios, simulations de décisions, plafonds et alertes de concentration."""
    fa = fin["fa_total"]
    lignes = [l for l in fin["lignes"] if l.get("dans_fa", True)]
    par_id = {l["id"]: l for l in lignes}
    expos = (etat.get("expositions") or {}) | (params.get("expositions_fonds") or {})
    if not expos:
        out_vide = "expositions des fonds absentes de l'état : transparence non évaluée"
    else:
        out_vide = None
    out = {"denominateur": fa, "avertissement": out_vide}
    env = {}
    for l in lignes:
        env[l["enveloppe"]] = env.get(l["enveloppe"], 0.0) + float(l["valeur_eur"])
    out["enveloppes"] = {k: {"valeur": v, "poids": v / fa} for k, v in sorted(env.items(), key=lambda kv: -kv[1])}
    classes = {}
    for l in lignes:
        v = float(l["valeur_eur"])
        t = l.get("type", "inconnu")
        lo, hi, _ = _bornes(l.get("part_actions", 1.0), 1.0)
        pa = (lo + hi) / 2           # point central d'une fourchette, signalé comme estimation
        if t in ("action", "etf", "fonds", "non_cote"):
            k = "non coté" if t == "non_cote" else "actions cotées"
            classes[k] = classes.get(k, 0.0) + v * pa
            if pa < 1:
                kk = "obligations et autres (fonds diversifiés, estimation)"
                classes[kk] = classes.get(kk, 0.0) + v * (1 - pa)
        else:
            lib = {"monetaire": "monétaire", "livret": "livrets", "liquidite": "liquidités", "crypto": "crypto",
                   "formule": "fonds à formule (capital garanti)"}.get(t, t)
            classes[lib] = classes.get(lib, 0.0) + v
    out["classes"] = {k: {"valeur": v, "poids": v / fa} for k, v in sorted(classes.items(), key=lambda kv: -kv[1])}
    out["pays"] = {k: {"valeur": v, "poids": v / fa} for k, v in sorted(repartition(lignes, expos, "pays").items(), key=lambda kv: -kv[1])}
    ex = expositions_emetteurs(lignes, expos)
    out["emetteurs"] = {k: {**v, "poids_min": v["total_min"] / fa} for k, v in sorted(ex["emetteurs"].items(), key=lambda kv: -kv[1]["total_min"])[:15]}
    out["fonds_partiels"] = ex["fonds_partiels"]
    directs = {l["id"]: float(l["valeur_eur"]) for l in lignes if l.get("type") == "action"}
    out["actions_directes"] = {"total": sum(directs.values()), "poids": sum(directs.values()) / fa,
                               "top5": top_n(directs, 5, fa), "hhi": hhi(list(directs.values()))}
    comptes_cto = set((params.get("fiscalite") or {}).get("comptes_titres_ordinaires") or [])
    cto = {l["id"]: float(l["valeur_eur"]) for l in lignes if l.get("enveloppe") in comptes_cto and l.get("type") == "action"}
    tot_cto = sum(float(l["valeur_eur"]) for l in lignes if l.get("enveloppe") in comptes_cto)
    if tot_cto:
        tri = sorted(cto.items(), key=lambda kv: -kv[1])
        out["poche_cto"] = {"total": tot_cto, "poids": {k: v / tot_cto for k, v in tri},
                            "top3": sum(v for _, v in tri[:3]) / tot_cto,
                            "themes": {nom: sum(cto.get(k, 0.0) for k in t.get("lignes", [])) / tot_cto
                                       for nom, t in (params.get("themes") or {}).items() if t.get("poche_cto")}}
    themes = {}
    for nom, d in (params.get("themes") or {}).items():
        dir_ = sum(float(par_id[i]["valeur_eur"]) for i in d.get("lignes", []) if i in par_id)
        themes[nom] = {"direct": dir_, "poids_direct": dir_ / fa, "definition": d.get("definition")}
    out["themes"] = themes
    hyp = params.get("hypotheses_stress", {})
    out["scenarios"] = [stress(lignes, s, hyp, fa) for s in params.get("scenarios", [])]
    for s in out["scenarios"]:
        s.pop("par_ligne", None)
    liq = params.get("liquidite", {})
    ldds = sum(float(l["valeur_eur"]) for l in lignes if l.get("type") == "livret")
    cc = sum(float(l["valeur_eur"]) for l in fin["lignes"] if l.get("id") == "COMPTE_COURANT")
    out["liquidite"] = liquidites_mobilisables(ldds, cc, liq.get("matelas_cible", 0.0), liq.get("reserve_fiscale", 0.0),
                                               liq.get("besoins_court_terme", 0.0), liq.get("compte_courant_dans_matelas", False))
    out["liquidite"]["mois_de_depenses"] = ldds / liq["depenses_mensuelles"] if liq.get("depenses_mensuelles") else None
    echelle = {}
    for l in fin["lignes"]:
        k = l.get("disponibilite", "inconnue")
        echelle[k] = echelle.get(k, 0.0) + float(l["valeur_eur"])
    out["echelle_liquidite"] = echelle
    ref = next((s for s in params.get("scenarios", []) if s["id"] == hyp.get("scenario_reference", "S3")), None)
    sims = []
    if ref:
        cumul = lignes
        for op in params.get("simulations_decisions", []):
            op2 = dict(op)
            op2["montant"] = _montant_operation(op, {l["id"]: l for l in cumul if True}, fa)
            if op2["montant"] <= 0:
                sims.append({"operation": op["libelle"], "sans_objet": True})
                continue
            base = cumul if op.get("cumulatif") else lignes
            try:
                r = simuler_decision(base, op2, hyp, ref, fa)
            except ValueError as e:
                sims.append({"operation": op["libelle"], "erreur": str(e)})
                continue
            r["montant"] = op2["montant"]
            cible = next((l for l in base if l["id"] == op.get("vers")), None)
            em = (cible or {}).get("emetteur")
            if em:
                d2 = fa + (op2["montant"] if op.get("financement_externe") else 0.0)
                ex2 = expositions_emetteurs(appliquer_operation(base, op2), expos)["emetteurs"].get(em)
                if ex2:
                    r["emetteur"] = {"nom": em, "poids_consolide_min_apres": ex2["total_min"] / d2,
                                     "poids_consolide_min_avant": (out["emetteurs"].get(em) or {}).get("poids_min"),
                                     "minimum": ex2.get("minimum", False)}
            if liq.get("compte_matelas") and op.get("de") == liq["compte_matelas"]:
                ld = next((float(l["valeur_eur"]) for l in base if l["id"] == liq["compte_matelas"]), None)
                if ld is not None:
                    r["ldds_apres"] = ld - op2["montant"]
                    r["ecart_matelas_apres"] = ld - op2["montant"] - liq.get("matelas_cible", 0.0)
            sims.append(r)
            if op.get("cumulatif"):
                cumul = appliquer_operation(cumul, op2)
    out["simulations"] = sims
    alertes = []
    for lid, pl in (params.get("plafonds_passifs") or {}).items():
        if lid in par_id:
            w = float(par_id[lid]["valeur_eur"]) / fa
            if w >= pl["plafond"] - pl.get("marge", 0.002):
                alertes.append({"ligne": lid, "poids": w, "plafond": pl["plafond"], "statut": pl.get("statut")})
    out["plafonds_passifs"] = alertes
    return out


def rapport_hebdomadaire(params: dict, res: dict) -> str:
    params = charger_params(params)
    d = res.get("diagnostic_complet")
    if not d:
        return ""
    L = ["## A. Synthèse exécutive", "",
         f"Patrimoine financier : **{fmt_eur(d['denominateur'])}** (dénominateur Finary).",
         "[[AGENT: évolution depuis la dernière exécution (gain financier vs flux), trois informations clés, état du risque]]", "",
         "## B. Alertes", "", "[[AGENT: reprendre le tableau d'état des règles et les événements de la semaine]]", "",
         "**Régime de marché (V2 §5)** — " + (ligne_regime(res.get("regime") or {}) or "valeurs non relevées"), "",
         "## C. Diagnostic patrimonial", "", "| Enveloppe | Valeur | Poids |", "| --- | --- | --- |"]
    L += [f"| {k} | {fmt_eur(v['valeur'], 0)} | {fmt_pct(v['poids'], 1)} |" for k, v in d["enveloppes"].items()]
    L += ["", "| Classe d'actifs | Valeur | Poids |", "| --- | --- | --- |"]
    L += [f"| {k} | {fmt_eur(v['valeur'], 0)} | {fmt_pct(v['poids'], 1)} |" for k, v in d["classes"].items()]
    L += ["", "| Pays (transparence) | Valeur | Poids |", "| --- | --- | --- |"]
    L += [f"| {k} | {fmt_eur(v['valeur'], 0)} | {fmt_pct(v['poids'], 1)} |" for k, v in list(d["pays"].items())[:8]]
    L += ["", "| Émetteur (direct + fonds) | Direct | Via fonds | Total | Poids |", "| --- | --- | --- | --- | --- |"]
    for k, v in list(d["emetteurs"].items())[:10]:
        pre = "≥ " if v.get("minimum") else ""
        L.append(f"| {k} | {fmt_eur(v['direct'], 0)} | {fmt_eur(v['indirect'], 0)} | {pre}{fmt_eur(v['total_min'], 0)} | {pre}{fmt_pct(v['poids_min'], 2)} |")
    pc = d.get("poche_cto")
    if pc:
        L += ["", "Poche CTO : " + " ; ".join(f"{k} {fmt_pct(v, 1)}" for k, v in pc["poids"].items())
              + f" ; trois premières {fmt_pct(pc['top3'], 1)}"
              + "".join(f" ; {nom} {fmt_pct(v, 1)}" for nom, v in pc.get("themes", {}).items()) + "."]
    a = d["actions_directes"]
    L += ["", f"Actions en direct : {fmt_eur(a['total'], 0)} ({fmt_pct(a['poids'], 1)}) ; 5 premières : {fmt_pct(a['top5']['poids'], 1)} ; "
          f"nombre effectif de lignes : {fmt_nombre(a['hhi']['n_effectif'], 1)}.", "",
          "## D. Scénarios (déterministes, hypothèses provisoires P-09)", "",
          "| Scénario | Impact | % du patrimoine |", "| --- | --- | --- |"]
    for s in d["scenarios"]:
        imp = fmt_eur(s["impact_min"], 0) if not s["fourchette"] else f"{fmt_eur(s['impact_min'], 0)} à {fmt_eur(s['impact_max'], 0)}"
        pc = fmt_pct(s["impact_pct_min"], 1) if not s["fourchette"] else f"{fmt_pct(s['impact_pct_min'], 1)} à {fmt_pct(s['impact_pct_max'], 1)}"
        L.append(f"| {s['libelle']} | {imp} | {pc} |")
    q = d["liquidite"]
    L += ["", f"Liquidités disponibles : {fmt_eur(q['disponible'], 0)} ; matelas cible {fmt_eur(q['matelas'], 0)} ; "
          f"réserve fiscale {fmt_eur(q['reserve_fiscale'], 0)} ; **mobilisable : {fmt_eur(q['mobilisable'], 0)}**"
          + (f" (manque {fmt_eur(-q['manque'], 0)})" if q['manque'] < 0 else "") + ".", "",
          "| Simulation de décision | Montant | Δ part actions | Δ perte S3 | Écart au matelas après | Émetteur consolidé après |",
          "| --- | --- | --- | --- | --- | --- |"]
    for s in d["simulations"]:
        if s.get("sans_objet") or s.get("erreur"):
            L.append(f"| {s['operation']} | — | {s.get('erreur', 'sans objet')} | | | |")
            continue
        dl = s["delta"]
        em = fmt_eur(s["ecart_matelas_apres"], 0) if s.get("ecart_matelas_apres") is not None else "inchangé"
        e2 = s.get("emetteur")
        e2t = (f"{e2['nom']} {'≥ ' if e2.get('minimum') else ''}{fmt_pct(e2['poids_consolide_min_apres'])}"
               + (f" (avant {fmt_pct(e2['poids_consolide_min_avant'])})" if e2.get("poids_consolide_min_avant") else "")) if e2 else "—"
        L.append(f"| {s['operation']} | {fmt_eur(s['montant'], 0)} | {fmt_pt(dl['part_actions'], 2)} | {fmt_eur(dl['perte_scenario_ref'], 0)} | {em} | {e2t} |")
    fi = res.get("fiscalite_annee")
    if fi:
        L += ["", f"Fiscalité de l'année (R12) : plus-values nettes {fmt_eur(fi['net_annee'], 0)}, base imposable "
              f"{fmt_eur(fi['base_imposable'], 0)}, impôt PFU ≈ {fmt_eur(fi['impot_pfu']['total'], 0)} ({fi['source']} ; {fi['perimetre']})."]
    L += ["", "## E. Recommandations", "",
          "[[AGENT: actions prioritaires conditionnelles (si X alors A), arbitrages, justification, impact en € et en points, fiscalité, niveau de confiance ; aucune recommandation sur une donnée NON ÉVALUÉE]]",
          "", "*Simulations et scénarios : ce ne sont pas des prévisions. Analyse, pas conseil réglementé.*"]
    return "\n".join(L)


def revaloriser(fin: dict, mesures: dict) -> dict:
    """Remplace la valeur Finary des lignes revalorisées (cours périmé d'un flux
    bancaire) et corrige le dénominateur d'autant, comme la V2 le faisait pour R1."""
    out = copy.deepcopy(fin)
    for m in mesures.values():
        if isinstance(m, dict) and m.get("ligne") and m.get("detail") and m.get("valeur") is not None:
            for l in out["lignes"]:
                if l["id"] == m["ligne"]:
                    l["valeur_eur_finary"] = l["valeur_eur"]
                    l["valeur_eur"] = m["detail"]["valeur_recalculee"]
                    l["revalorisee"] = True
            out["fa_total"] = m["detail"]["denominateur_recalcule"]
            out["fa_total_finary"] = fin.get("fa_total")
    return out


def chiffrer_action(rid: str, p: dict, r: dict, fin: dict, mesures: dict, params: dict) -> dict | None:
    """Quantités, montants et impôt de l'action conditionnelle d'une règle ÉLIGIBLE ou
    DÉCLENCHÉE, au cours disponible (clôture revalorisée si elle existe, sinon Finary)."""
    if p["type"] in ("plafonds", "interdiction", "rappels", "resultats_sans_regle"):
        return None            # la V2 ne prévoit qu'une alerte, aucune action chiffrée
    if r.get("etat", "").split("/")[0] not in ("ELIGIBLE", "DECLENCHEE") and "ELIGIBLE" not in r.get("etat", ""):
        return None
    taux = dict(TAUX_DEFAUT, **(params.get("fiscalite", {}).get("taux") or {}))
    tmi = params.get("fiscalite", {}).get("tmi_foyer", 0.11)
    fa = fin.get("fa_total")
    lid = p.get("ligne")
    lig = next((l for l in fin.get("lignes", []) if l.get("id") == lid), None)
    out = {"regle": rid}
    if p["type"] in ("tranches_drawdown",):
        tr = [t for t in r.get("tranches", []) if t.get("etat") == "ELIGIBLE"]
        out["tranches"] = [{"id": t["id"],
                            "montant": t.get("montant_effectif", next((x["montant"] for x in p["tranches"] if x["id"] == t["id"]), None)),
                            "plafonne": "montant_effectif" in t, "controle_liquidite": t.get("controle_liquidite")} for t in tr]
        return out
    if not lig or not fa:
        out["indisponible"] = "ligne ou dénominateur absent"
        return out
    q = float(lig.get("quantite") or 0)
    v, d = float(lig["valeur_eur"]), fa
    m = mesures.get(p.get("mesure", "")) or {}
    if m.get("detail"):
        v, d = m["detail"]["valeur_recalculee"], m["detail"]["denominateur_recalcule"]
    prix = v / q if q else None
    pru = lig.get("pru_eur")
    out.update({"ligne": lid, "cours_eur": prix, "poids": v / d, "quantite_detenue": q})

    def fisc(n):
        if pru is None or prix is None:
            return {"plus_value": None}
        pv = plus_value(n, prix, float(pru))
        f = {"plus_value": pv}
        if pv > 0:
            f["pfu"] = impot_pfu(pv, taux)["total"]
            f["bareme_approx"] = impot_bareme_approx(pv, tmi, taux)["total_net"]
        else:
            f["moins_value_imputable"] = -pv
            f["economie_pfu_si_imputee"] = -pv * (taux["pfu_ir"] + taux["ps"])
        return f
    if p["type"] == "seuil_poids":
        n = titres_a_vendre(v, prix, p["cible_allegement"], d)
        out.update({"sens": "vente", "titres": n, "montant": n * prix, "poids_apres": (v - n * prix) / d, **fisc(n)})
    elif p["type"] in ("resultats",) and r.get("cible") is not None:
        n = titres_a_vendre(v, prix, r["cible"], d)
        out.update({"sens": "vente", "cible": r["cible"], "titres": n, "montant": n * prix, "poids_apres": (v - n * prix) / d, **fisc(n)})
    elif p["type"] in ("resultats_vente", "sortie"):
        n = int(q)
        out.update({"sens": "vente totale", "titres": n, "montant": n * prix, **fisc(n),
                    "quantite_a_confirmer": p["type"] == "resultats_vente"})
    elif p["type"] == "renfort":
        n = titres_a_acheter(v, prix, p["cible"], d, financement_hors_fa=True)
        cap = next((q for q in params.get("regles", {}).values() if q.get("type") == "plafond_consolide"), None)
        cons = (mesures.get("_consolide") or {}).get("emetteurs", {}).get(lig.get("emetteur") or "")
        plafonne = None
        if cap and cons and prix:
            dc = (mesures.get("_consolide") or {}).get("denominateur") or d
            x_max = (cap["seuil"] * dc - cons["total_min"]) / (1 - cap["seuil"])     # achat financé hors du dénominateur
            n_max = max(0, math.floor(x_max / prix - 1e-9))
            if n > n_max:
                n, plafonne = n_max, f"plafonné par R14 (poids consolidé ≤ {fmt_pct(cap['seuil'], 0)})"
            out["poids_consolide_apres"] = (cons["total_min"] + n * prix) / (dc + n * prix)
        out.update({"sens": "achat", "cible": p["cible"], "titres": n, "montant": n * prix,
                    "poids_apres": (v + n * prix) / (d + n * prix), "financement": p.get("financement", "à préciser")})
        if plafonne:
            out["plafonne"] = plafonne
    return out


def composer_notifications(params: dict, res: dict) -> list:
    """Textes prêts à envoyer. Push : une ligne < 200 caractères, sans montant
    (écran verrouillé). Message : chiffres du moteur + marqueurs pour l'agent."""
    out = []
    regles = params.get("regles", {})
    rodage = bool(res.get("rodage"))
    for e in (res.get("rodage_aurait_notifie", []) if rodage else res.get("a_envoyer", [])):
        rid = e["regle"]
        p = regles.get(rid, {})
        lib = p.get("libelle", rid)
        r = res["regles"].get(rid, {}) if rid in res.get("regles", {}) else {}
        push = {"DECLENCHEE": f"{rid} {lib} : seuil atteint ({e['detail'][:40]}). Voir le rapport.",
                "ELIGIBLE": f"{rid} {lib} : ÉLIGIBLE ({e['detail'][:40]}). Aucun ordre passé.",
                "TOUCHE_GELE": f"{rid} {lib} {e['detail']} : seuil touché, gelé jusqu'au {fmt_date(p.get('garde_date'), False)}.",
                "AGGRAVATION": f"{rid} {lib} : aggravation depuis la dernière alerte.",
                "RESULTATS_PUBLIES": f"Résultats publiés : {e['detail']}. Analyse dans le rapport.",
                "RUPTURE_REGIME": "Rupture de régime de marché détectée. Voir le rapport.",
                "DONNEE_CRITIQUE_MANQUANTE": f"Donnée critique manquante 2 jours de suite : {e['detail']}.",
                "LIGNE_INTERDITE": f"{rid} : ligne interdite détectée ({e['detail']}).",
                "VEILLE": f"{rid} {lib} : veille déclenchée ({e['detail'][:60]})."}.get(e["type"], f"{rid} : {e['type']}")
        push = ("[RODAGE, non envoyé] " if rodage else "") + f"[{e['id']}] " + push
        corps = [f"**{rid} · {lib} — {LIBELLES_ETATS.get(e['type'], e['type'].replace('_', ' '))}** (séance du {fmt_date(e['seance'])}, réf. {e['id']})",
                 "", "**Verdict :** [[AGENT: une phrase, action conditionnelle, jamais un ordre]]", "",
                 "**Raisonnement**"]
        mes = res.get("mesures", {})
        if r.get("valeur") is not None:
            nom = (mes.get(p.get("mesure", "")) or {}).get("mesure")
            corps.append(f"- Mesure{' (' + nom + ')' if nom else ''} : {fmt_pct(r['valeur'])}"
                         + (f" ; seuil {fmt_pct(r.get('seuil'))}" if r.get("seuil") else ""))
            comp = mes.get(p.get("mesure_observation", "")) or {}
            if comp.get("valeur") is not None:
                borne = (mes.get(p.get("mesure", "")) or {}).get("borne")
                corps.append(f"- Pour comparaison, {comp.get('mesure', 'autre mesure')} : {fmt_pct(comp['valeur'])}, écart "
                             f"{fmt_nombre((r['valeur'] - comp['valeur']) * 100, 2)} pt (la V2 citait une marge de ±0,5 pt)"
                             + (f" ; mesure de la règle : {borne}." if borne else "."))
        der = (mes.get(p.get("mesure", "")) or {}).get("derive") if p.get("type") == "seuil_poids" else None
        if der:
            corps.append(f"- Dérive depuis le {fmt_date(der['depuis'], False)} : {fmt_nombre(der['variation'] * 100, 2)} pt, dont ligne "
                         f"{fmt_nombre(der['effet_ligne'] * 100, 2)} pt et reste du patrimoine {fmt_nombre(der['effet_reste'] * 100, 2)} pt. "
                         f"Poids si seule la ligne avait bougé : {fmt_pct(der['v2_seule_ligne'])} ; si seul le reste avait bougé : "
                         f"{fmt_pct(der['v2_seul_reste'])}.")
            if der["cause"] == "reste du patrimoine" and der["effet_reste"] > 0:
                corps.append("- Cause : la dérive vient surtout du reste du patrimoine ; la vente est optionnelle (V2).")
        for t in r.get("tranches", []) if e["type"] in ("ELIGIBLE", "TOUCHE_GELE") else []:
            if t["id"] == e["detail"] and "montant_effectif" in t:
                liq_p = params.get("liquidite") or {}
                corps.append(f"- Montant de la tranche : {fmt_eur(t['montant_effectif'], 0) if t['montant_effectif'] is not None else 'non calculable (solde inconnu)'}"
                             f" (D1 : excédent au-delà du matelas de {fmt_eur(liq_p.get('matelas_cible'), 0)} et de la réserve fiscale de "
                             f"{fmt_eur(liq_p.get('reserve_fiscale'), 0)}, partagé entre les tranches restantes ; au plus "
                             f"{fmt_eur(next((x['montant'] for x in p['tranches'] if x['id'] == t['id']), None), 2)}).")
            if t["id"] == e["detail"] and t.get("controle_liquidite"):
                c = t["controle_liquidite"]
                compte = (params.get("liquidite") or {}).get("compte_matelas", "compte")
                montant_t = c.get("montant_tranche")
                seule = montant_t is not None and abs(c["tranches_comprises"] - montant_t) < 0.01
                portee = "après cette tranche" if seule else f"après cette tranche et les précédentes non consommées ({fmt_eur(c['tranches_comprises'], 0)} au total)"
                corps.append(f"- Contrôle de liquidité (information) : {compte} {fmt_eur(c['solde_avant'], 0)} → {fmt_eur(c['solde_apres'], 0)} "
                             f"{portee}, soit {fmt_eur(c['ecart_matelas'], 0)} par rapport au matelas cible de {fmt_eur(c['matelas'], 0)}.")
        a = (res.get("actions_chiffrees") or {}).get(rid)
        if a and a.get("titres") is not None:
            ligne = (f"- Action chiffrée par le moteur : {a['sens']} de {a['titres']} titre(s) {a.get('ligne', '')} "
                     f"≈ {fmt_eur(a.get('montant'), 0)} au cours de {fmt_eur(a.get('cours_eur'))}")
            if a.get("poids_apres") is not None:
                ligne += f", poids après {fmt_pct(a['poids_apres'])}"
            if a.get("plus_value") is not None:
                ligne += f" ; plus-value {fmt_eur(a['plus_value'], 0)}"
                if a.get("pfu") is not None:
                    ligne += f", impôt PFU {fmt_eur(a['pfu'], 0)} (barème ≈ {fmt_eur(a['bareme_approx'], 0)})"
                if a.get("economie_pfu_si_imputee") is not None:
                    ligne += f", moins-value imputable, économie ≈ {fmt_eur(a['economie_pfu_si_imputee'], 0)} si des plus-values existent"
            if a.get("quantite_a_confirmer"):
                ligne += " ; quantité totale par défaut, à confirmer (P-07)"
            if a.get("poids_consolide_apres") is not None:
                ligne += f" ; poids consolidé après ≥ {fmt_pct(a['poids_consolide_apres'])}" + (f", {a['plafonne']}" if a.get("plafonne") else "")
            if a.get("financement"):
                ligne += f" ; financement : {a['financement']}"
            corps.append(ligne + ".")
        corps += ["- [[AGENT: cause, données et sources, niveau de confiance]]", "",
                  "**Action conditionnelle :** [[AGENT: si … alors …, impact en € et en points, fiscalité]]", "",
                  "*Aucun ordre n'a été passé. Analyse, pas conseil réglementé.*"]
        out.append({"id": e["id"], "push": push[:199], "message": "\n".join(corps)})
    return out


def bilan_fiscal_annee(params: dict, collecte: dict, seance, diag: dict | None = None) -> dict:
    """R12 — plus-values nettes de l'année à partir de l'export des cessions fourni par
    l'utilisateur (Finary n'a pas l'historique), reports imputés, impôt au PFU, et
    économie possible en réalisant une moins-value latente avant le 31/12.
    Périmètre : les comptes-titres de l'utilisateur ; ceux de sa partenaire ne sont pas
    suivis, alors que l'imputation se fait sur tout le foyer fiscal s'ils sont imposés ensemble."""
    annee = lire_date(seance).year
    taux = dict(TAUX_DEFAUT, **((params.get("fiscalite") or {}).get("taux") or {}))
    cess, erreurs = [], []
    for c in collecte.get("cessions") or []:
        pv = c.get("plus_value")
        try:
            if pv is None:
                pv = plus_value(float(c["quantite"]), float(c["prix_cession_eur"]), float(c["pru_eur"]), float(c.get("frais_eur", 0)))
            cess.append({**c, "plus_value": float(pv)})
        except (KeyError, TypeError, ValueError):
            erreurs.append(f"cession illisible : {c}")
    b = bilan_plus_values(cess, collecte.get("moins_values_reportables") or [], annee)
    b["impot_pfu"] = impot_pfu(b["base_imposable"], taux)
    b["cessions_comptees"] = len([c for c in cess if lire_date(c["date"]).year == annee])
    lat = (diag or {}).get("latents_cto") or {}
    b["recolte_possible"] = {k: economie_recolte_moins_value(v, b["base_imposable"], taux["pfu_ir"] + taux["ps"])
                             for k, v in sorted(lat.items(), key=lambda kv: kv[1]) if v < 0}
    b["source"] = collecte.get("cessions_source") or "export des cessions fourni par l'utilisateur"
    b["perimetre"] = "tes comptes-titres seulement ; ceux de ta partenaire ne sont pas suivis"
    b["taux_verifies_le"] = taux.get("verifie_le")
    b["erreurs"] = erreurs
    return b


def diagnostic_leger(fin: dict, params: dict) -> dict:
    fa = fin.get("fa_total")
    env = {}
    for c in fin.get("comptes", []):
        if c.get("dans_fa", True):
            env[c["alias"]] = env.get(c["alias"], 0.0) + float(c["valeur"])
    latents = {}
    for l in fin.get("lignes", []):
        if l.get("pru_eur") is not None and l.get("quantite") and l.get("enveloppe", "").startswith("CTO"):
            latents[l["id"]] = float(l["valeur_eur"]) - float(l["quantite"]) * float(l["pru_eur"])
    return {"fa_total": fa, "brut": fin.get("brut"), "latents_cto": latents,
            "enveloppes": {k: {"valeur": v, "poids": v / fa if fa else None} for k, v in sorted(env.items(), key=lambda kv: -kv[1])}}


def executer(params: dict, etat_prec: dict | None, collecte: dict, sauvegardes: list | None = None) -> dict:
    """Pipeline complet d'une exécution. Ne modifie pas ses arguments."""
    params = charger_params(params)
    run = collecte["run"]
    maintenant = lire_instant(run["maintenant"])
    etat0, origine = restaurer(copy.deepcopy(etat_prec) if etat_prec else None, sauvegardes or [])
    sans_etat = origine.startswith("état vide")
    mode = determiner_mode(maintenant, etat0.get("derniere_seance_officielle"), etat0.get("verrou"), run.get("run_id"))
    seance = mode["seance_cible"]
    etat = copy.deepcopy(etat0)
    fin = normaliser_finary(collecte.get("finary", {}), params)
    collecte = dict(collecte, finary_normalise=fin)
    anomalies = controler_finary(fin, etat0, params, maintenant) if fin else [
        {"code": "FINARY_INDISPONIBLE", "gravite": "HAUTE", "texte": "relevé Finary absent"}]
    mesures, marche, valid = calculer_mesures(params, etat0, collecte, seance, maintenant)
    seances_integrees = mesures.pop("_seances_integrees", {})
    initialisations = mesures.pop("_initialisations", [])
    soldes = {c["alias"]: float(c["valeur"]) for c in fin.get("comptes", [])}
    for l in fin.get("lignes", []):
        if l.get("solde_suivi"):
            soldes[l["solde_suivi"]] = float(l["valeur_eur"])
    consolide = poids_consolides(fin, (etat0.get("expositions") or {}) | (collecte.get("expositions") or {})
                                 | (params.get("expositions_fonds") or {}))
    if consolide:
        mesures["_consolide"] = consolide
    ctx = {"seance": seance, "mode": mode["mode"], "mesures": mesures, "faits": collecte.get("faits", {}), "consolide": consolide,
           "confirmations": collecte.get("confirmations", {}), "soldes": soldes,
           "liquidite": params.get("liquidite", {}),
           "soldes_precedents": (etat0.get("patrimoine") or {}).get("soldes"),
           "lignes": fin.get("lignes", []), "seance_precedente_officielle": etat0.get("derniere_seance_officielle")}
    resultats = evaluer_regles(params.get("regles", {}), etat0.get("regles", {}), ctx)
    # Données critiques manquantes : compteur de jours consécutifs
    meta_compteurs = etat.setdefault("compteurs", {})
    compteurs = meta_compteurs.setdefault("manquant", {})
    officiel = mode["mode"] in ("OFFICIEL", "RATTRAPAGE")
    deja_compte = meta_compteurs.get("seance_comptee") == seance      # une séance ne compte qu'une fois
    alertes_donnees = []
    valid_c = dict(valid)
    valid_c["FINARY"] = {"valeur": fin.get("fa_total"), "qualite": "PROBABLE" if fin.get("fa_total") else "MANQUANTE"}
    for cle in params.get("donnees_critiques", []) + ["FINARY"]:
        v = valid_c.get(cle) or {}
        if v.get("valeur") is None or v.get("qualite") in ("MANQUANTE", "REJETEE", "DIVERGENTE"):
            if officiel and not deja_compte:
                compteurs[cle] = compteurs.get(cle, 0) + 1
                if compteurs[cle] == 2:
                    alertes_donnees.append(cle)      # une seule notification, au 2e jour consécutif
        else:
            compteurs[cle] = 0
    if officiel:
        meta_compteurs["seance_comptee"] = seance
    # Aucune donnée de marché critique pour la séance cible : la séance n'est pas marquée traitée,
    # une exécution ultérieure (même soir ou lendemain) la reprendra.
    critiques_marche = [c for c in params.get("donnees_critiques", []) if c in params.get("instruments", {})
                        and not params["instruments"][c].get("anteriorite_max_jours")]   # clôtures datées de la séance
    seance_vide = officiel and bool(critiques_marche) and all((valid.get(c) or {}).get("valeur") is None for c in critiques_marche)
    if seance_vide:
        anomalies.append({"code": "SEANCE_SANS_DONNEES", "gravite": "HAUTE",
                          "texte": f"aucune donnée de marché critique pour la séance du {fmt_date(seance)} : séance non marquée comme traitée"})
    # Régime
    reg_cour = {}
    for k, cle in (("us10y", "US10Y"), ("brent", "BRENT"), ("vix", "VIX")):
        v = valid.get(cle) or {}
        reg_cour[k] = v.get("valeur") if v.get("qualite") not in ("MANQUANTE", "REJETEE", "DIVERGENTE", "DOUTEUSE") else None
    reg_cour["date"] = seance
    reg_prec = (etat0.get("regime") or {}).get("dernier")
    reg = rupture_regime(reg_cour, reg_prec,
                         (etat0.get("regime") or {}).get("reference"), params.get("regime", {}).get("seuils", {
                             "us10y_pb": 25, "brent_pct": 0.15, "vix_haut": 40, "vix_bas": 20}))
    reg.update({"courant": reg_cour, "precedent": reg_prec, "reference": (etat0.get("regime") or {}).get("reference"),
                "eurusd": (valid.get("EURUSD") or {}).get("valeur")})
    # Événements et boîte d'envoi
    nouveaux = []
    ecrire = mode["mode"] in ("OFFICIEL", "RATTRAPAGE", "ALLEGE")
    for rid, r in resultats.items():
        for ev in r["evenements"]:
            notif = ev["type"] in EVENEMENTS_NOTIFIES and not r.get("mode_observation")
            if ev["type"] == "SORTIE_ELIGIBLE":
                notif = bool(params.get("notifier_sortie_eligible", False))
            evt = {"id": cle_evenement(rid, ev["type"], seance, ev.get("detail", "")), "regle": rid,
                   "type": ev["type"], "detail": ev.get("detail", ""), "seance": seance,
                   "notifier": notif, "cree_le": maintenant.isoformat()}
            nouveaux.append(evt)
    if reg["rupture_v2"]:
        nouveaux.append({"id": cle_evenement("REGIME", "RUPTURE_REGIME", seance), "regle": "§5",
                         "type": "RUPTURE_REGIME", "detail": " ; ".join(reg["motifs_v2"]), "seance": seance,
                         "notifier": True, "cree_le": maintenant.isoformat()})
    for cle in alertes_donnees:
        nouveaux.append({"id": cle_evenement("DONNEES", "DONNEE_CRITIQUE_MANQUANTE", seance, cle), "regle": "§2",
                         "type": "DONNEE_CRITIQUE_MANQUANTE", "detail": cle, "seance": seance,
                         "notifier": True, "cree_le": maintenant.isoformat()})
    if sans_etat:
        for e in nouveaux:
            e["sans_etat"] = True
    crees = []
    rodage = bool(run.get("rodage"))      # rodage parallèle : rien n'est envoyé, la V2 reste officielle
    if ecrire:
        if collecte.get("acquittements"):
            acquitter(etat, collecte["acquittements"], maintenant.isoformat())
        for e in nouveaux:
            ev, cree = ajouter_evenement(etat, dict(e, statut="RODAGE") if rodage and e.get("notifier") else e)
            if cree:
                crees.append(ev["id"])
        # Mise à jour de l'état des règles
        for rid, r in resultats.items():
            prec = etat.setdefault("regles", {}).get(rid, {}) or {}
            nv = {k: v for k, v in r.items() if k in ("etat", "tranches", "dernier_notifie", "publication_notifiee", "lignes", "cible",
                                                      "revue", "clause", "emetteurs")}
            nv["etat"] = r["etat"] if r.get("evaluee", True) else prec.get("etat", r["etat"])
            if nv.get("etat") != prec.get("etat"):
                nv["depuis"] = seance
            else:
                nv["depuis"] = prec.get("depuis", seance)
            for k in ("dernier_notifie", "publication_notifiee", "reference", "criteres_remplis", "suivi"):
                if k not in nv and k in prec:
                    nv[k] = prec[k]
            if r.get("criteres_remplis") is not None:
                nv["criteres_remplis"] = r["criteres_remplis"]
            pm = params["regles"][rid]
            mw = mesures.get(pm.get("mesure", "")) if pm["type"] == "seuil_poids" else None
            if mw and mw.get("valeur") is not None and mode["mode"] in ("OFFICIEL", "RATTRAPAGE"):
                nv["suivi"] = {"seance": seance, "poids": mw["valeur"],
                               "denominateur": mw["detail"]["denominateur_recalcule"]}
            etat["regles"][rid] = nv
        etat["marche"] = marche
        if collecte.get("expositions"):
            etat.setdefault("expositions", {}).update(collecte["expositions"])
        dernier0 = (etat0.get("regime") or {}).get("dernier") or {}
        dernier = dict(dernier0)
        for k in ("us10y", "brent", "vix"):
            if reg_cour.get(k) is not None:          # une valeur absente ne remplace pas la dernière connue
                dernier[k] = reg_cour[k]
                dernier.setdefault("dates", {})
                dernier["dates"] = dict(dernier.get("dates") or {}, **{k: seance})
        dernier["date"] = seance if any(reg_cour.get(k) is not None for k in ("us10y", "brent", "vix")) else dernier0.get("date")
        etat["regime"] = {"reference": (etat0.get("regime") or {}).get("reference"), "dernier": dernier}
        if fin:
            suivis = {p.get("compte_source") for p in params.get("regles", {}).values() if p.get("compte_source")}
            suivis.add((params.get("liquidite") or {}).get("compte_matelas"))
            etat["patrimoine"] = {"fa_total": fin.get("fa_total"), "horodatage": fin.get("horodatage"),
                                  "soldes": {k: v for k, v in soldes.items() if k in suivis}}
        if mode["mode"] in ("OFFICIEL", "RATTRAPAGE") and not seance_vide:
            etat["derniere_seance_officielle"] = seance
        etat["run_seq"] = int(etat0.get("run_seq", 0)) + 1
        resolues = set(collecte.get("anomalies_resolues", []))
        anciennes = {a.get("code", "") + "|" + a.get("texte", "")[:40]: a for a in etat0.get("anomalies", [])}
        nouvelles = [a for a in etat0.get("anomalies", []) if a.get("origine") == "V2" and a.get("id") not in resolues]
        for a in anomalies:
            k = a.get("code", "") + "|" + a.get("texte", "")[:40]
            prec = anciennes.get(k)
            nouvelles.append(dict(a, ouverte_le=(prec or {}).get("ouverte_le", seance)))
        etat["anomalies"] = nouvelles
        # Accumulateur de performance (flux déclarés par l'utilisateur ; sinon aucun flux supposé, signalé)
        if fin.get("fa_total") and mode["mode"] in ("OFFICIEL", "RATTRAPAGE"):
            flux = sum(float(f["montant"]) for f in collecte.get("flux", []) if f.get("externe", True))
            etat["performance"] = maj_indice_twr(etat0.get("performance"), seance, fin["fa_total"], flux)
            if not collecte.get("flux_confirmes", False):
                etat["performance"]["avertissement"] = "flux externes non confirmés : performance provisoire"
        etat["verrou"] = None
        etat.setdefault("journal_runs", []).append({"run_id": run.get("run_id"), "instant": maintenant.isoformat(),
                                                    "mode": mode["mode"], "seance": seance,
                                                    "evenements_crees": crees, "version_moteur": VERSION})
        etat["journal_runs"] = etat["journal_runs"][-60:]
        archiver(etat, seance)
        sceller(etat)
    verifs = [v for r in resultats.values() for v in r.get("verifications", [])]
    for cle in params.get("donnees_critiques", []):
        v = valid.get(cle) or {}
        if v.get("recoupement_insuffisant") and not any(cle in x.get("cle", []) for x in verifs):
            verifs.append({"cle": [cle], "raison": "recoupement exigé par la V2 §2 : " + v["recoupement_insuffisant"]})
        elif v.get("valeur") is not None and not qualite_suffit_pour_transition(v) and not any(cle in x.get("cle", []) for x in verifs):
            verifs.append({"cle": [cle], "raison": "source primaire citée telle quelle, ou seconde source, nécessaire"})
    diag = diagnostic_leger(fin, params) if fin else {}
    fin_rev = revaloriser(fin, mesures) if fin else {}
    actions = {}
    for rid, r in resultats.items():
        a = chiffrer_action(rid, params["regles"][rid], r, fin_rev, mesures, params) if fin else None
        if a:
            actions[rid] = a
    fiscalite = bilan_fiscal_annee(params, collecte, seance, diag) if collecte.get("cessions") is not None else None
    complet = None
    if fin and (mode["hebdomadaire"] or run.get("forcer_hebdomadaire")):
        complet = diagnostic_complet(revaloriser(fin, mesures), params, etat, mesures)
    a_envoyer = [] if rodage else [e for e in evenements_a_envoyer(etat if ecrire else etat0) if e["id"] in crees or ecrire]
    aurait_notifie = [e for e in (etat if ecrire else etat0).get("evenements", [])
                      if rodage and e["id"] in crees and e.get("notifier")]
    return {"version_moteur": VERSION, "mode": mode, "origine_etat": origine, "sans_etat": sans_etat,
            "mesures": mesures, "validations": valid, "regles": resultats, "regime": reg,
            "initialisations": initialisations, "diagnostic_complet": complet, "actions_chiffrees": actions,
            "seances_integrees": seances_integrees, "seance_non_traitee": seance_vide,
            "anomalies": anomalies, "verifications_requises": verifs, "diagnostic": diag, "fiscalite_annee": fiscalite,
            "evenements_crees": crees, "a_envoyer": a_envoyer, "rodage": rodage, "rodage_aurait_notifie": aurait_notifie,
            "etat": etat if ecrire else etat0, "etat_modifie": ecrire,
            "sauvegardes": rotation_sauvegardes(sauvegardes or [], etat0) if ecrire and not sans_etat else (sauvegardes or [])}


# ════════════════════════════════════════════════════════════════════════════
# 16. Rapports (brouillons chiffrés ; l'agent remplace les marqueurs [[AGENT: …]])
# ════════════════════════════════════════════════════════════════════════════

def ligne_etat_regle(rid, p, r, mesures=None):
    etat = r["etat"]
    lib = " / ".join(LIBELLES_ETATS.get(x, x) for x in etat.split("/"))
    if not r.get("evaluee", True):
        lib += " (non évaluée)"
    val = seuil = dist = ""
    if p["type"] == "seuil_poids" and r.get("valeur") is not None:
        val, seuil, dist = fmt_pct(r["valeur"]), "≥ " + fmt_pct(p["seuil"], 1), fmt_pt(r["distance_pt"])
        m = (mesures or {}).get(p["mesure"]) or {}
        if m.get("hausse_requise") is not None:
            dist += f" (cours +{fmt_pct(m['hausse_requise'], 1)}, soit {fmt_nombre(m['prix_seuil_eur'])} €)"
    elif p["type"] == "tranches_drawdown" and r.get("valeur") is not None:
        val = fmt_pct(r["valeur"], 1)
        seuil = " ; ".join(f"{t['id']} {fmt_pct(t['seuil'], 0)}" for t in p["tranches"])
        dist = " ; ".join(f"{t['id']} " + (f"franchi de {fmt_pt(-t['distance_pt'], 1)}" if t["distance_pt"] <= 0 else fmt_pt(t["distance_pt"], 1))
                          for t in r.get("tranches", []) if "distance_pt" in t)
    elif p["type"] == "renfort" and r.get("valeur") is not None:
        val, seuil = fmt_pct(r["valeur"], 1) + " de la référence", "≤ " + fmt_pct(p["ratio_max"], 0)
        dist = fmt_pct(r["distance_pct"], 1)
    elif p["type"] == "plafond_consolide" and r.get("valeur") is not None:
        val, seuil = f"{r.get('premier', '')} ≥ {fmt_pct(r['valeur'])}", "≥ " + fmt_pct(p["seuil"], 0)
        dist = fmt_pt(p["seuil"] - r["valeur"])
    elif p["type"] == "plafonds":
        val = " ; ".join(f"{k} {fmt_pct(v.get('poids'))}" for k, v in (r.get("lignes") or {}).items() if v.get("poids") is not None)
        seuil = " ; ".join(f"{k} {fmt_pct(q['plafond'], 1)}" for k, q in p["lignes"].items())
    if not val and r.get("notes"):
        val = r["notes"][0]
    return f"| {rid} · {p.get('libelle', '')} | {val} | {seuil} | {dist} | {lib} |"


def ligne_regime(reg: dict) -> str:
    """Panneau de régime (V2 §5) : valeurs du jour, écart au dernier état et au contexte de référence."""
    c, p, ref = reg.get("courant") or {}, reg.get("precedent") or {}, reg.get("reference") or {}
    if not c:
        return ""
    def ecart(k, unite):
        out = []
        for nom, b in (("dernier état", p), (fmt_date(ref.get("date"), False) if ref.get("date") else "référence", ref)):
            if c.get(k) is not None and b.get(k) is not None:
                out.append(f"{fmt_nombre((c[k] - b[k]) * 100, 0)} pb vs {nom}" if unite == "pb"
                           else f"{fmt_pct(c[k] / b[k] - 1, 1)} vs {nom}")
        return f" ({' ; '.join(out)})" if out else ""
    morceaux = [f"10 ans US {fmt_nombre(c.get('us10y'), 2)} %{ecart('us10y', 'pb')}" if c.get("us10y") is not None else "10 ans US non relevé",
                f"Brent {fmt_nombre(c.get('brent'), 1)} ${ecart('brent', '%')}" if c.get("brent") is not None else "Brent non relevé",
                f"VIX {fmt_nombre(c.get('vix'), 1)}" if c.get("vix") is not None else "VIX non relevé"]
    if reg.get("eurusd") is not None:
        morceaux.append(f"EUR/USD BCE {fmt_nombre(reg['eurusd'], 4)}")
    morceaux.append("Fed : [[AGENT: anticipation du prochain mouvement, source datée]]")
    txt = " · ".join(morceaux)
    txt += (" · **rupture (V2) : " + " ; ".join(reg["motifs_v2"]) + "**") if reg.get("rupture_v2") else " · pas de rupture (V2)"
    if reg.get("derive_v3"):
        txt += (f" · dérive depuis le {fmt_date(ref.get('date'), False)}" if ref.get("date") else " · dérive depuis la référence") \
            + " (observation V3) : " + " ; ".join(reg["motifs_v3"])
    return txt


def rapport_quotidien(params: dict, res: dict) -> str:
    params = charger_params(params)
    m = res["mode"]
    fin = res.get("diagnostic", {})
    if res.get("rodage"):
        aurait = res.get("rodage_aurait_notifie") or []
        bandeau = ("**RODAGE — aucune notification envoyée ; la V2 reste officielle.** La V3 aurait notifié : "
                   + ("; ".join(f"{e['regle']} {LIBELLES_ETATS.get(e['type'], e['type'])} ({e['detail'][:40]})" for e in aurait)
                      if aurait else "rien") + ". [[AGENT: comparaison avec l'exécution V2 du jour]]")
    else:
        bandeau = None
    tete = (f"**Séance de référence : {fmt_date(m['seance_cible'])}** · mode {m['mode']}"
            f"{' · données J-1' if m['donnees_j_moins_1'] else ''}"
            f"{' · SANS ÉTAT' if res['sans_etat'] else ''} · patrimoine financier "
            f"{fmt_eur(fin.get('fa_total'))} · [[AGENT: fraîcheur Finary par établissement]]")
    mes = res.get("mesures", {})
    r1 = next((p for p in params.get("regles", {}).values() if p["type"] == "seuil_poids"), None)
    jour = []
    if r1 and mes.get(r1["mesure"], {}).get("valeur") is not None:
        mm = mes[r1["mesure"]]
        d = mm["detail"]
        cours = next((c for c in mm.get("composantes", []) if c.get("cle") == r1["instrument"]), {})
        fxv = next((c for c in mm.get("composantes", []) if c.get("cle") == "EURUSD"), {})
        jour.append(f"{r1['instrument']} {fmt_nombre(cours.get('valeur'))} $ soit {fmt_nombre(d['cours_eur'])} € (EUR/USD BCE "
                    f"{fmt_nombre(fxv.get('valeur'), 4)}) ; poids {fmt_pct(mm['valeur'])} (Finary {fmt_pct(d['poids_finary'])}) ; "
                    f"distance à {fmt_pct(r1['seuil'], 1)} : {fmt_pt(r1['seuil'] - mm['valeur'])}")
    dd = mes.get("dd_eur_spx") or {}
    v2 = (mes.get("dd_eur_spx_formule_v2") or {}).get("valeur")
    if dd.get("valeur") is not None:
        jour.append(f"S&P 500 : DD_EUR {fmt_pct(dd['valeur'])} (baisse exacte en EUR, {dd.get('borne', '')}) ; "
                    f"DD_USD {fmt_pct(mes.get('dd_usd_spx', {}).get('valeur'))}" + (f" ; formule V2 {fmt_pct(v2)}" if v2 is not None else ""))
    prochains = [rr["prochain"] for rr in res["regles"].values() if rr.get("prochain")]
    if prochains:
        d0 = min(x["date"] for x in prochains)
        jour.append(f"Prochain : {fmt_date(d0, False)} ({' ; '.join(x['libelle'] for x in prochains if x['date'] == d0)})")
    regime = ligne_regime(res.get("regime") or {})
    rattrapage = []
    manquees = [d for d in m.get("seances_a_traiter", []) if d != m["seance_cible"]]
    if m["mode"] == "RATTRAPAGE" and manquees:
        integ = res.get("seances_integrees") or {}
        vues = sorted({d for v in integ.values() for d in v})
        sans = [d for d in manquees if d not in vues]
        txt = "**Rattrapage** — "
        if vues:
            txt += ("clôtures intégrées : " + " ; ".join(f"{fmt_date(d, False)} ({', '.join(sorted(k for k, v in integ.items() if d in v))})"
                                                     for d in vues) + ". ")
        if sans:
            txt += f"Séance(s) sans clôtures datées : {', '.join(fmt_date(d, False) for d in sans)} ; plus hauts et variations possiblement incomplets. "
        rattrapage = [txt + "Règles évaluées sur la séance cible seulement (aucun relevé Finary daté des séances manquées).", ""]
    lignes = ([bandeau, ""] if bandeau else []) + [tete, ""] + (["**Ligne du jour** — " + " · ".join(jour), ""] if jour else []) + rattrapage + (
        ["**Régime** — " + regime, ""] if regime else []) + ["**Changé depuis la dernière exécution**",
              "[[AGENT: 5 lignes au plus, à partir de `regles`, `anomalies` et `evenements_crees`]]", "",
              "| Règle | Valeur | Seuil | Distance | Statut |", "| --- | --- | --- | --- | --- |"]
    for rid, p in params.get("regles", {}).items():
        lignes.append(ligne_etat_regle(rid, p, res["regles"][rid], res.get("mesures")))
    actives = [c for c in params.get("contraintes_datees", []) if lire_date(m["seance_cible"]) < lire_date(c["jusqu_au"])]
    if actives:
        lignes += ["", "**Contraintes en vigueur :** " + " ; ".join(c["texte"] for c in actives)]
    fi = res.get("fiscalite_annee")
    rappel_r12 = any(n.startswith("rappel du") for rid, rr in res["regles"].items()
                     if params["regles"][rid].get("fiscal") for n in rr.get("notes", []))
    if fi:
        rec = " ; ".join(f"{k} {fmt_eur(lat_v, 0)} (économie ≈ {fmt_eur(x['economie'], 0)} si réalisée avant le 31/12)"
                         for k, x in fi["recolte_possible"].items()
                         for lat_v in [((res.get("diagnostic") or {}).get("latents_cto") or {}).get(k)])
        lignes += ["", f"**Fiscalité {lire_date(m['seance_cible']).year} (R12)** — {fi['source']} : {fi['cessions_comptees']} cession(s), "
                   f"plus-values nettes {fmt_eur(fi['net_annee'], 0)} ; reports imputés {fmt_eur(sum(x['montant'] for x in fi['imputations']), 0)} ; "
                   f"base imposable {fmt_eur(fi['base_imposable'], 0)} ; impôt PFU ≈ {fmt_eur(fi['impot_pfu']['total'], 0)} "
                   f"(taux vérifiés le {fmt_date(fi['taux_verifies_le'])}). Périmètre : {fi['perimetre']}."
                   + (f" Moins-values latentes : {rec}." if rec else "")]
    elif rappel_r12:
        lignes += ["", "**Fiscalité (R12)** — rappel du jour : l'export des cessions de l'année n'est pas fourni. "
                   "Demande-le (Finary n'a pas l'historique des cessions)."]
    lat = (res.get("diagnostic") or {}).get("latents_cto") or {}
    if lat:
        lignes += ["", "**Latents CTO (cours Finary) :** " + " ; ".join(f"{k} {fmt_eur(v, 0)}" for k, v in sorted(lat.items(), key=lambda kv: kv[1]))]
    if res["verifications_requises"]:
        lignes += ["", "**Vérifications demandées par le moteur**"] + [
            f"- {', '.join(v['cle'])} : {v['raison']}" for v in res["verifications_requises"]]
    lignes += ["", "[[AGENT: sources (titre et lien)]]", "", "*Analyse, pas conseil réglementé.*"]
    return "\n".join(lignes)


def marqueurs_restants(texte: str) -> list:
    out, i = [], 0
    while True:
        a = texte.find("[[AGENT:", i)
        if a < 0:
            return out
        b = texte.find("]]", a)
        out.append(texte[a:b + 2])
        i = b + 2


# ════════════════════════════════════════════════════════════════════════════
# 17. Auto-test et ligne de commande
# ════════════════════════════════════════════════════════════════════════════

def selftest() -> dict:
    """Vecteurs de contrôle : un écart signale un fichier corrompu ou mal recopié."""
    ok = []
    ok.append(("pâques 2026", paques(2026) == dt.date(2026, 4, 5)))
    ok.append(("thanksgiving 2026 fermé", not est_seance(dt.date(2026, 11, 26))))
    ok.append(("27/11/2026 écourtée", dt.date(2026, 11, 27) in fermetures_anticipees_nyse(2026)))
    ok.append(("clôture US 27/10/2026 = 21h Paris",
               heure_cloture(dt.date(2026, 10, 27)).astimezone(PARIS).hour == 21))
    ok.append(("DD EUR formule V2 (données publiques)",
               abs(drawdown_eur_formule_v2(6910.39, 7818.93, 1.1269, 1.0990) - (-0.09376)) < 5e-5))
    ok.append(("prix seuil", abs(prix_seuil(100, 0.04, 240000) - 100.0) < 1e-9))
    ok.append(("titres à vendre", titres_a_vendre(4400, 100, 0.04, 100000) == 4))
    ok.append(("file max glissante", maj_fenetre_max([{"date": "2026-01-02", "v": 5}], "2026-01-03", 7)[0]["v"] == 7))
    ok.append(("empreinte canonique", empreinte({"b": 1, "a": [1, 2]}) == empreinte(json.loads('{"a":[1,2],"b":1}'))))
    ok.append(("TWR", abs(twr([("a", 100000, 0), ("b", 105000, 10000), ("c", 120000, 0)]) - 0.0956522) < 1e-6))
    ok.append(("PFU 31,4 %", abs(impot_pfu(1000)["total"] - 314.0) < 1e-9))
    return {"ok": all(x for _, x in ok), "controles": ok, "version": VERSION}


def _lire(chemin):
    if chemin in (None, "", "-"):
        return None
    with open(chemin, encoding="utf-8") as f:
        return json.load(f)


def _ecrire(chemin, obj):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)


def texte_journal(obj) -> str:
    """Texte JSON d'un état destiné à l'appel d'écriture du journal : ASCII pur (accents et signes notés
    \\uXXXX), donc rien d'ambigu à recopier (« − » contre « - », espaces insécables). Le document enregistre
    les caractères décodés (constaté le 24/11/2026) : relu, c'est le même objet, donc la même empreinte."""
    s = json.dumps(obj, ensure_ascii=True, indent=1, sort_keys=False)
    return s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def empreinte_bloc(obj) -> str:
    """Empreinte courte que le document renvoie pour un bloc de code (« h ») : 8 premiers caractères
    hexadécimaux du SHA-256 du texte enregistré (JSON décodé, indentation 1). Constaté le 24/11/2026 sur
    « {} », « [] » et les deux blocs du journal ; propriété non documentée : en cas d'écart persistant,
    contrôler par relecture et verifier-etat."""
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")).hexdigest()[:8]


def _ecrire_journal(chemin, obj):
    with open(chemin, "w", encoding="ascii") as f:
        f.write(texte_journal(obj) + "\n")


def controle_params(p: dict, attendue: str) -> dict:
    """Le bloc de paramètres recopié doit avoir exactement l'empreinte annoncée par le prompt.
    Une clé « empreinte » absente ou modifiée ne suffit pas à passer le contrôle."""
    calc = empreinte(p)
    att = (attendue or "").strip().lower()
    if not att:
        return {"conforme": False, "empreinte": calc, "motif": "empreinte attendue non fournie"}
    if calc != att:
        return {"conforme": False, "empreinte": calc, "attendue": att, "motif": "bloc différent de celui du prompt (mal recopié ou modifié)"}
    if p.get("empreinte") not in (None, att):
        return {"conforme": False, "empreinte": calc, "attendue": att, "motif": "empreinte déclarée dans le bloc incohérente"}
    return {"conforme": True, "empreinte": calc, "motif": ""}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="moteur_v3")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("selftest")
    sp.add_parser("version")
    a = sp.add_parser("calendrier")
    a.add_argument("--maintenant", required=True)
    a.add_argument("--derniere-officielle")
    a = sp.add_parser("executer")
    a.add_argument("--params", required=True)
    a.add_argument("--empreinte-params", required=True, help="empreinte attendue, recopiée de l'en-tête du prompt")
    a.add_argument("--etat")
    a.add_argument("--sauvegardes")
    a.add_argument("--collecte", required=True)
    a.add_argument("--sortie", required=True)
    a = sp.add_parser("confirmer")
    a.add_argument("--etat", required=True)
    a.add_argument("--evt", required=True)
    a.add_argument("--canal", required=True, choices=["message", "push"])
    a.add_argument("--succes", required=True, choices=["oui", "non"])
    a.add_argument("--instant", required=True)
    a.add_argument("--detail", default="")
    a = sp.add_parser("acquitter")
    a.add_argument("--etat", required=True)
    a.add_argument("--ids", required=True)
    a.add_argument("--instant", required=True)
    a = sp.add_parser("verifier-etat")
    a.add_argument("--etat", required=True)
    a = sp.add_parser("migrer-v2")
    a.add_argument("--extrait", required=True)
    a.add_argument("--sortie", required=True)
    a = sp.add_parser("empreinte-params")
    a.add_argument("--params", required=True)
    a.add_argument("--attendue", required=True, help="empreinte recopiée de l'en-tête du prompt")
    a = sp.add_parser("verifier-rapport")
    a.add_argument("--rapport", required=True)
    a = sp.add_parser("controle-bloc", help="compare l'empreinte « h » renvoyée par le document au texte attendu")
    a.add_argument("--fichier", required=True)
    a.add_argument("--h", required=True, help="valeur h de l'accusé d'écriture du bloc")
    a.add_argument("--liste", action="store_true")
    a = sp.add_parser("contenu-bloc", help="chaîne JSON prête à coller dans l'appel d'écriture du journal")
    a.add_argument("--fichier", required=True, help="état (etat.json) ou liste de sauvegardes")
    a.add_argument("--liste", action="store_true", help="enveloppe l'état dans une liste (onglet Sauvegardes)")
    x = ap.parse_args(argv)
    if x.cmd == "selftest":
        r = selftest()
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1
    if x.cmd == "version":
        print(json.dumps({"version": VERSION, "sha256_fichier": empreinte_fichier(__file__)}))
        return 0
    if x.cmd == "calendrier":
        print(json.dumps(determiner_mode(x.maintenant, x.derniere_officielle), ensure_ascii=False, indent=1))
        return 0
    if x.cmd == "empreinte-params":
        p = _lire(x.params)
        ok = controle_params(p, x.attendue)
        print(json.dumps(ok, ensure_ascii=False))
        return 0 if ok["conforme"] else 2
    if x.cmd == "verifier-rapport":
        with open(x.rapport, encoding="utf-8") as f:
            restants = marqueurs_restants(f.read())
        print(json.dumps({"marqueurs_restants": restants, "conforme": not restants}, ensure_ascii=False))
        return 0 if not restants else 1
    if x.cmd == "executer":
        params = _lire(x.params)
        ok = controle_params(params, x.empreinte_params)
        if not ok["conforme"]:
            print(json.dumps({"erreur": "paramètres refusés : " + ok["motif"]}, ensure_ascii=False))
            return 2
        res = executer(params, _lire(x.etat), _lire(x.collecte), _lire(x.sauvegardes) or [])
        import os
        os.makedirs(x.sortie, exist_ok=True)
        _ecrire_journal(os.path.join(x.sortie, "etat.json"), res["etat"])
        _ecrire_journal(os.path.join(x.sortie, "sauvegardes.json"), res["sauvegardes"])
        _ecrire(os.path.join(x.sortie, "resultat.json"), {k: v for k, v in res.items() if k not in ("etat", "sauvegardes")})
        _ecrire(os.path.join(x.sortie, "notifications.json"), composer_notifications(params, res))
        with open(os.path.join(x.sortie, "rapport_brouillon.md"), "w", encoding="utf-8") as f:
            f.write(rapport_quotidien(params, res))
            if res.get("diagnostic_complet"):
                f.write("\n\n" + rapport_hebdomadaire(params, res))
        print(json.dumps({"mode": res["mode"]["mode"], "seance": res["mode"]["seance_cible"],
                          "evenements_crees": res["evenements_crees"],
                          "verifications_requises": len(res["verifications_requises"]),
                          "empreinte_etat": res["etat"].get("empreinte")}, ensure_ascii=False))
        return 0
    if x.cmd == "confirmer":
        e = _lire(x.etat)
        ev = confirmer_envoi(e, x.evt, x.canal, x.succes == "oui", x.instant, x.detail)
        sceller(e)
        _ecrire_journal(x.etat, e)
        print(json.dumps({"evt": ev["id"], "statut": ev["statut"], "empreinte_etat": e["empreinte"]}, ensure_ascii=False))
        return 0
    if x.cmd == "acquitter":
        e = _lire(x.etat)
        n = acquitter(e, [i.strip() for i in x.ids.split(",") if i.strip()], x.instant)
        sceller(e)
        _ecrire_journal(x.etat, e)
        print(json.dumps({"acquittes": n, "empreinte_etat": e["empreinte"]}))
        return 0
    if x.cmd == "verifier-etat":
        r = verifier_etat(_lire(x.etat))
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1
    if x.cmd == "controle-bloc":
        obj = _lire(x.fichier)
        if x.liste and not isinstance(obj, list):
            obj = [obj]
        att = empreinte_bloc(obj)
        ok = x.h.strip().lower() == att
        print(json.dumps({"conforme": ok, "attendu": att, "recu": x.h.strip().lower()}))
        return 0 if ok else 1
    if x.cmd == "contenu-bloc":
        obj = _lire(x.fichier)
        if x.liste and not isinstance(obj, list):
            obj = [obj]
        print(json.dumps("```json\n" + texte_journal(obj) + "\n```", ensure_ascii=True))
        return 0
    if x.cmd == "migrer-v2":
        e = migrer_v2(_lire(x.extrait))
        _ecrire_journal(x.sortie, e)
        print(json.dumps({"empreinte": e["empreinte"]}))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
