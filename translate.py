# -*- coding: utf-8 -*-
"""
Traduction FR d'un JSON Foundry/5eTools-like en conservant la même arborescence.

Caractéristiques :
- Ajoute name_en = valeur originale de name (si présent)
- Remplace name par la traduction FR
- Traduit "tous" les champs textuels, sauf sous-arbres techniques (ex. system)
- Protège tokens : [[...]], {@...}, @item.*, dés XdY, /save...
- Traduit certains enums via mapping : activities[].type, activation.type, damage.onSave, effects[].statuses
- Glossaire FR léger sur noms/descriptions
- DeepL avec cache et batching

Variables d'env :
- TRANSLATE_PROVIDER=deepl (obligatoire : ce script est paramétré pour DeepL)
- TRANSLATE_API_KEY=<clé>
- (optionnel) DEEPL_API_BASE=https://api-free.deepl.com  (ou https://api.deepl.com)
- (optionnel) TRANSLATE_SRC=EN (défaut), TRANSLATE_TGT=FR (défaut)
"""

import os
import re
import json
import time
import argparse
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import requests
from tqdm import tqdm

# ---------------------- Réglages généraux ----------------------
DEFAULT_SRC = "EN"
DEFAULT_TGT = "FR"
BATCH_SIZE = 30  # segments par appel
SLEEP = 0.6  # pause entre appels pour éviter 429
CACHE_PATH = Path("translate_cache.json")

# ---------------------- Zones à ignorer ------------------------
# Clés dont on NE traduit pas les valeurs (identifiants, codes, etc.)
SKIP_VALUE_KEYS = {
    "foundryId", "uuid", "id", "img", "icon", "iconPath", "tag", "tags",
    "slug", "key", "module", "pack", "path", "file",
    "source", "sources",  # code/source de livre
    "duration.seconds",
    "dc.calculation", "calculation",
    "mode", "denomination", "number",
    "school", "level", "scaling", "target.affects.count",
    "ability", "abilities",  # codes d'abilités (str courts)
    # NOTE: on NE met PAS "type" ici : on veut le traduire via ENUM_MAP
}
# Sous-arbres entiers à ne pas traduire
SKIP_SUBTREES = {
    "system",  # bloc technique Foundry
}

# ---------------------- Tokens à protéger ----------------------
RE_FOUNDRY_BLOCK = re.compile(r"\[\[.*?\]\]")  # [[ ... ]]
RE_5ETOOLS_TAG = re.compile(r"{@[^{}]+}")  # {@...}
RE_AT_TOKEN = re.compile(r"@[A-Za-z0-9_.\[\]-]+")  # @item.level, @abilities.con.mod, etc.
RE_DICE = re.compile(r"\b\d+d\d+([+-]\d+)?\b")  # 3d8, 2d6+3
RE_COMMAND = re.compile(r"/[a-zA-Z]+")  # /save, /roll, ...


def protect_tokens(text: str) -> Tuple[str, List[str]]:
    if not text:
        return "", []
    tokens: List[str] = []
    out = text
    for regex in (RE_FOUNDRY_BLOCK, RE_5ETOOLS_TAG, RE_AT_TOKEN, RE_DICE, RE_COMMAND):
        def _sub(m):
            token = f"§§T{len(tokens)}§§"
            tokens.append(m.group(0))
            return token

        out = regex.sub(_sub, out)
    return out, tokens


def restore_tokens(text: str, tokens: List[str]) -> str:
    out = text
    for i, tok in enumerate(tokens):
        out = out.replace(f"§§T{i}§§", tok)
    return out


def norm_ws(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").replace("\r\n", "\n")).strip()


# ---------------------- Cache ----------------------
def load_cache() -> Dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: Dict[str, str]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_key(provider: str, src: str, tgt: str, text: str) -> str:
    return hashlib.sha256(f"{provider}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()


# ---------------------- DeepL ----------------------
class DeepLTranslator:
    def __init__(self, api_key: str, src: str, tgt: str):
        self.api_key = api_key
        self.src = src
        self.tgt = tgt

    def _endpoint(self) -> str:
        base = os.getenv("DEEPL_API_BASE", "").strip().rstrip("/")
        if base:
            return base + "/v2/translate"
        # auto-détection simple : clés "Free" finissent souvent par :fx
        if (self.api_key or "").endswith(":fx"):
            return "https://api-free.deepl.com/v2/translate"
        return "https://api.deepl.com/v2/translate"

    def translate_batch(self, texts: List[str]) -> List[str]:
        ep = self._endpoint()
        form = [("text", t) for t in texts]
        params = {"source_lang": self.src, "target_lang": self.tgt, "preserve_formatting": "1"}
        headers = {
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        r = requests.post(ep, data=form, params=params, headers=headers, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"DeepL HTTP {r.status_code}: {r.text[:400]}")
        js = r.json()
        return [it["text"] for it in js.get("translations", [])]


def make_translator() -> DeepLTranslator:
    provider = (os.getenv("TRANSLATE_PROVIDER") or "deepl").lower()
    if provider != "deepl":
        raise RuntimeError("Ce script est paramétré pour DeepL uniquement (TRANSLATE_PROVIDER=deepl).")
    api_key = os.getenv("TRANSLATE_API_KEY") or ""
    if not api_key:
        raise RuntimeError("TRANSLATE_API_KEY manquant.")
    src = (os.getenv("TRANSLATE_SRC") or DEFAULT_SRC).upper()
    tgt = (os.getenv("TRANSLATE_TGT") or DEFAULT_TGT).upper()
    return DeepLTranslator(api_key, src, tgt)


# ---------------------- Traduction par lots ----------------------
def translate_segments(translator: DeepLTranslator, segs: List[str], cache: Dict[str, str]) -> List[str]:
    prepared: List[str] = []
    tokenlists: List[List[str]] = []
    for s in segs:
        s = norm_ws(s)
        p, toks = protect_tokens(s)
        prepared.append(p)
        tokenlists.append(toks)

    results: List[Union[str, None]] = [None] * len(prepared)
    to_call: List[str] = []
    idxs: List[int] = []

    for i, s in enumerate(prepared):
        key = cache_key("deepl", translator.src, translator.tgt, s)
        if s and key in cache:
            results[i] = restore_tokens(cache[key], tokenlists[i])
        else:
            to_call.append(s)
            idxs.append(i)

    for start in range(0, len(to_call), BATCH_SIZE):
        part = to_call[start:start + BATCH_SIZE]
        if not part:
            continue
        translated = translator.translate_batch(part)
        for j, tr in enumerate(translated):
            i_glob = idxs[start + j]
            fixed = restore_tokens(tr, tokenlists[i_glob])
            results[i_glob] = fixed
            key = cache_key("deepl", translator.src, translator.tgt, prepared[i_glob])
            cache[key] = fixed
        save_cache(cache)
        time.sleep(SLEEP)

    return [r if r is not None else "" for r in results]


# ---------------------- Mappings / Glossaire ----------------------
# Enums (en contexte)
ENUM_MAP = {
    # activities[].type
    ("activities.type", "damage"): "dégâts",
    ("activities.type", "save"): "sauvegarde",
    ("activities.type", "healing"): "soin",
    ("activities.type", "utility"): "utilitaire",

    # activation.type
    ("activation.type", "action"): "action",
    ("activation.type", "bonus"): "action bonus",
    ("activation.type", "reaction"): "réaction",
    ("activation.type", "minute"): "minute",
    ("activation.type", "hour"): "heure",
    ("activation.type", ""): "",

    # damage.onSave
    ("damage.onSave", "none"): "aucun",
    ("damage.onSave", "half"): "moitié",
}

# Status de conditions : si usage purement "imprimé", c'est OK de traduire
STATUS_MAP = {
    "blinded": "aveuglé",
    "deafened": "assourdi",
    "charmed": "charmé",
    "frightened": "terrorisé",
    "paralyzed": "paralysé",
    "poisoned": "empoisonné",
    "prone": "à terre",
    "restrained": "entravé",
    "stunned": "hébété",
    "unconscious": "inconscient",
    "poison": "poison",
}

# Glossaire FR (léger) pour corriger certains titres/mots récurrents
GLOSSARY_REPLACE = {
    "Smite": "Châtiment",
    "Blinding": "Aveuglant",
    "Blindness": "Cécité",
    "Deafness": "Surdité",
    "and": "et",  # utile dans certains noms composés
    "radiant": "rayonnant"
}


def apply_glossary_fr(text: str) -> str:
    out = text
    for en, fr in GLOSSARY_REPLACE.items():
        out = out.replace(en, fr)
    return out


# ---------------------- Parcours & collecte ----------------------
DotPath = str


def should_skip_value(dotpath: DotPath, key: str) -> bool:
    if key in SKIP_VALUE_KEYS:
        return True
    # Si l'un des segments du chemin appartient aux sous-arbres ignorés, on skip tout
    parts = dotpath.split(".") if dotpath else []
    if any(p in SKIP_SUBTREES for p in parts):
        return True
    return False


def collect_strings(obj: Any, dotpath: DotPath, segments: List[str], anchors: List[Tuple[DotPath, Union[int, str]]]):
    """
    Parcourt l'objet et collecte tous les strings à traduire.
    - Exclut les sous-arbres "system" et les clés SKIP_VALUE_KEYS.
    - N'exclut PAS "type" (il sera traduit et/ou corrigé via ENUM_MAP).
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{dotpath}.{k}" if dotpath else k
            if isinstance(v, str):
                if should_skip_value(dotpath, k):
                    continue
                segments.append(v)
                anchors.append((dotpath, k))
            else:
                collect_strings(v, child_path, segments, anchors)

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child_path = f"{dotpath}.{i}" if dotpath else str(i)
            if isinstance(v, str):
                parts = dotpath.split(".") if dotpath else []
                if any(p in SKIP_SUBTREES for p in parts):
                    continue
                segments.append(v)
                anchors.append((dotpath, i))
            else:
                collect_strings(v, child_path, segments, anchors)


def set_translated(root: Any, dotpath: DotPath, key_or_idx: Union[str, int], value: str):
    """Réinjecte la valeur traduite au bon endroit du document (dict ou list)."""
    if dotpath == "":
        parent = root
    else:
        parent = root
        for p in dotpath.split("."):
            if p == "":
                continue
            if p.isdigit():
                parent = parent[int(p)]
            else:
                parent = parent[p]
    if isinstance(key_or_idx, int):
        parent[key_or_idx] = value
    else:
        parent[key_or_idx] = value


# ---------------------- Post-traitements ----------------------
def walk_and_postprocess(obj: Any, dotpath: DotPath = ""):
    """
    Post-process :
    - enums (type, activation.type, damage.onSave) via ENUM_MAP
    - statuses via STATUS_MAP
    - glossaire FR (name, description, et dans listes d'effets/activités)
    """
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            child = f"{dotpath}.{k}" if dotpath else k

            # a) ENUMS en contexte
            if k == "type" and isinstance(v, str):
                # activities.type
                if dotpath.endswith(".activities") or ".activities." in dotpath:
                    obj[k] = ENUM_MAP.get(("activities.type", v), v)
                # activation.type
                if dotpath.endswith(".activation") or ".activation." in dotpath:
                    obj[k] = ENUM_MAP.get(("activation.type", v), obj[k])

            if k == "onSave" and isinstance(v, str) and ".damage" in dotpath:
                obj[k] = ENUM_MAP.get(("damage.onSave", v), v)

            # b) STATUSES -> mapping FR
            if k == "statuses" and isinstance(v, list):
                obj[k] = [STATUS_MAP.get(s, s) for s in v]

            # c) Glossaire FR pour name/description
            if isinstance(v, str) and k in {"name", "description"}:
                obj[k] = apply_glossary_fr(v)

            # récurrence
            walk_and_postprocess(v, child)

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            child = f"{dotpath}.{i}" if dotpath else str(i)
            if isinstance(v, str):
                if any(seg in dotpath for seg in (".effects.", ".activities.")):
                    obj[i] = apply_glossary_fr(v)
            walk_and_postprocess(v, child)


# ---------------------- Pipeline principal ----------------------
def process_file(in_path: str, out_path: str):
    data = json.loads(Path(in_path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Le JSON racine doit être une liste (array).")

    translator = make_translator()
    cache = load_cache()

    # 1) Capturer name_en AVANT toute traduction
    for sp in data:
        if isinstance(sp, dict) and isinstance(sp.get("name"), str):
            if not sp.get("name_en"):
                sp["name_en"] = sp["name"]

    # 2) Collecter tous les strings à traduire (hors parties techniques)
    segments: List[str] = []
    anchors: List[Tuple[DotPath, Union[int, str]]] = []
    collect_strings(data, "", segments, anchors)

    # 3) Traduire par lots
    print(f"Segments à traduire: {len(segments)}")
    translated: List[str] = []
    for start in tqdm(range(0, len(segments), BATCH_SIZE)):
        part = segments[start:start + BATCH_SIZE]
        translated.extend(translate_segments(translator, part, cache))

    # 4) Réinjecter
    it = iter(translated)
    for (dotpath, key_or_idx) in anchors:
        set_translated(data, dotpath, key_or_idx, next(it))

    # 5) Post-traitements : enums, statuses, glossaire
    walk_and_postprocess(data)

    # 6) Écrire
    Path(out_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    save_cache(cache)
    print(f"OK: {out_path} (objets: {len(data)})")


# ---------------------- CLI ----------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Traduction FR complète (même arborescence), ajout name_en.")
    ap.add_argument("--in", dest="in_path", required=True, help="JSON d'entrée (array d'objets).")
    ap.add_argument("--out", dest="out_path", required=True, help="JSON de sortie.")
    args = ap.parse_args()
    process_file(args.in_path, args.out_path)
