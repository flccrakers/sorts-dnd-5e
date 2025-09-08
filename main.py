# -*- coding: utf-8 -*-
"""
Collecte des sorts 5eTools depuis le miroir GitHub, agrégation et export CSV/JSON.
Dépendances: requests, pandas (optionnelle mais pratique pour le CSV).
> pip install requests pandas
"""

import json
import time
from pathlib import Path

import requests
import pandas as pd

GITHUB_API_DIR = "https://api.github.com/repos/5etools-mirror-3/5etools-src/contents/data/spells"
OUT_DIR = Path("5etools_spells_dump")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def list_spell_json_files():
    """Retourne la liste des items (avec download_url) du dossier data/spells/."""
    r = requests.get(GITHUB_API_DIR, timeout=30)
    r.raise_for_status()
    items = r.json()
    # On garde uniquement les .json “spells-*.json”
    return [it for it in items if it.get("name", "").endswith(".json")]


def fetch_json(url):
    """Télécharge un JSON brut via une URL de type download_url renvoyée par l’API GitHub."""
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()


def extract_spells_from_payload(payload, source_filename=None):
    """
    Les fichiers de 5eTools ont généralement la clé "spell": [ ... ].
    On ajoute quelques métadonnées utiles (source_filename, source book si dispo).
    """
    spells = []
    if isinstance(payload, dict):
        arr = payload.get("spell") or payload.get("spells") or []
        for s in arr:
            s = dict(s)  # copie
            s["_src_file"] = source_filename
            # Essayer de récupérer une info de source lisible si présente
            meta = payload.get("_meta", {})
            s["_book"] = (meta.get("sources") or [{}])[0].get("json", None)
            spells.append(s)
    elif isinstance(payload, list):
        # Moins probable, mais on supporte le cas list top-level
        for s in payload:
            if isinstance(s, dict):
                s = dict(s)
                s["_src_file"] = source_filename
                spells.append(s)
    return spells


def flatten_for_csv(spell):
    """
    Prépare un dict “plat” minimal pour le CSV. Le JSON complet est conservé séparément.
    Les clés varient selon les versions (PHB'14 vs PHB'24). On gère les plus communes.
    """
    return {
        "name": spell.get("name"),
        "level": spell.get("level"),
        "school": spell.get("school"),
        "time": "; ".join(
            f'{t.get("number", "")} {t.get("unit", "")}'.strip()
            for t in spell.get("time", [])
        ) if isinstance(spell.get("time"), list) else spell.get("time"),
        "range": spell.get("range", {}).get("distance", {}).get("amount")
        if isinstance(spell.get("range"), dict) else spell.get("range"),
        "components": ",".join(sorted(k for k, v in spell.get("components", {}).items() if v is True))
        if isinstance(spell.get("components"), dict) else spell.get("components"),
        "duration": "; ".join(
            d.get("type", "") for d in spell.get("duration", [])
        ) if isinstance(spell.get("duration"), list) else spell.get("duration"),
        "classes": ", ".join(spell.get("classes", {}).get("fromClassList", []))
        if isinstance(spell.get("classes"), dict) else None,
        "source": spell.get("source"),
        "_src_file": spell.get("_src_file"),
        "_book": spell.get("_book"),
    }


def main():
    files = list_spell_json_files()
    print(f"Fichiers de sorts trouvés: {len(files)}")

    all_spells = []
    for i, it in enumerate(files, 1):
        url = it.get("download_url")
        name = it.get("name")
        if not url:
            continue
        print(f"[{i}/{len(files)}] Téléchargement: {name}")
        payload = fetch_json(url)
        spells = extract_spells_from_payload(payload, source_filename=name)
        print(f"  -> {len(spells)} sorts")
        all_spells.extend(spells)
        # Doux throttle pour ne pas heurter les limites non authentifiées
        time.sleep(0.3)

    print(f"Total sorts agrégés: {len(all_spells)}")

    # Sauvegarde JSON “complet”
    with open(OUT_DIR / "spells_5etools_full.json", "w", encoding="utf-8") as f:
        json.dump(all_spells, f, ensure_ascii=False, indent=2)

    # Sauvegarde CSV simplifié
    rows = [flatten_for_csv(s) for s in all_spells]
    df = pd.DataFrame(rows)
    df.sort_values(by=["level", "name"], inplace=True, ignore_index=True)
    df.to_csv(OUT_DIR / "spells_5etools_min.csv", index=False, encoding="utf-8")
    print(f"Fichiers écrits dans: {OUT_DIR.resolve()}")


if __name__ == "__main__":
    main()
