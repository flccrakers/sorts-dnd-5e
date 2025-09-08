# -*- coding: utf-8 -*-
"""
Traduction automatique des sorts 5eTools vers le français.

- Lit un JSON (array d'objets sort) exporté de 5eTools (ex: spells_5etools_full.json)
- Traduit: name -> name_fr, school -> school_fr, entries/desc -> desc_fr
- Préserve les tags 5eTools: {@...} (placeholder -> restauration)
- Fournit 3 providers: DeepL, Google, Azure (via variables d'env)
- Met en cache les phrases traduites (translate_cache.json) pour accélérer
- Écrit un nouveau JSON 'spells_translated.json' exploitable par votre script Scribus

Dépendances: requests, tqdm
"""

import os
import re
import json
import time
import base64
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Tuple

import requests
from tqdm import tqdm

# ---------- Config globale ----------
DEFAULT_SRC_LANG = "EN"
DEFAULT_TGT_LANG = "FR"
CACHE_PATH = Path("translate_cache.json")

# Batching sécurité (évite les 429)
BATCH_SIZE = 30  # nb. de segments par appel (selon provider)
SLEEP_BETWEEN_CALLS = 0.6

# ---------- Helpers tags 5eTools ----------
TAG_RE = re.compile(r"{@[^{}]+}")  # ex: {@spell Fireball|phb}


def protect_tags(text: str) -> Tuple[str, List[str]]:
    """Remplace les balises {@...} par des marqueurs non traduisibles."""
    tags = TAG_RE.findall(text or "")
    out = text or ""
    for i, tag in enumerate(tags):
        token = f"§§TAG{i}§§"
        out = out.replace(tag, token, 1)
    return out, tags


def restore_tags(text: str, tags: List[str]) -> str:
    out = text
    for i, tag in enumerate(tags):
        token = f"§§TAG{i}§§"
        out = out.replace(token, tag)
    return out


def normalize_whitespace(s: str) -> str:
    return re.sub(r"[ \t]+", " ", (s or "").replace("\r\n", "\n")).strip()


# ---------- Cache simple ----------
def load_cache() -> Dict[str, str]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(cache: Dict[str, str]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_key(text: str, provider: str, src: str, tgt: str) -> str:
    h = hashlib.sha256(f"{provider}|{src}|{tgt}|{text}".encode("utf-8")).hexdigest()
    return h


# ---------- Providers ----------
class BaseTranslator:
    def __init__(self, api_key: str, src_lang: str, tgt_lang: str):
        self.api_key = api_key
        self.src_lang = src_lang
        self.tgt_lang = tgt_lang

    def translate_batch(self, texts: List[str]) -> List[str]:
        raise NotImplementedError


class DeepLTranslator(BaseTranslator):
    # Utilise api-free.deepl.com ou api.deepl.com selon votre clé
    def translate_batch(self, texts: List[str]) -> List[str]:
        endpoint = "https://api.deepl.com/v2/translate"
        if self.api_key.startswith("free:") or os.getenv("DEEPL_FREE") == "1":
            endpoint = "https://api-free.deepl.com/v2/translate"
        data = []
        for t in texts:
            data.append(("text", t))
        payload = {
            "source_lang": self.src_lang,
            "target_lang": self.tgt_lang,
            "preserve_formatting": "1",
        }
        r = requests.post(endpoint, data=data, params=payload,
                          headers={"Authorization": f"DeepL-Auth-Key {self.api_key}"}, timeout=60)
        r.raise_for_status()
        js = r.json()
        return [item["text"] for item in js.get("translations", [])]


class GoogleTranslator(BaseTranslator):
    # Google Cloud Translation v2 (endpoint REST simple)
    def translate_batch(self, texts: List[str]) -> List[str]:
        endpoint = "https://translation.googleapis.com/language/translate/v2"
        payload = {
            "q": texts,
            "source": self.src_lang.lower(),
            "target": self.tgt_lang.lower(),
            "format": "text",
            "model": "nmt",
            "key": self.api_key,
        }
        r = requests.post(endpoint, json=payload, timeout=60)
        r.raise_for_status()
        js = r.json()
        return [x["translatedText"] for x in js["data"]["translations"]]


class AzureTranslator(BaseTranslator):
    # Microsoft Translator (Azure Cognitive Services)
    def translate_batch(self, texts: List[str]) -> List[str]:
        region = os.getenv("TRANSLATE_REGION")
        if not region:
            raise RuntimeError("TRANSLATE_REGION requis pour Azure (ex: westeurope).")
        endpoint = f"https://api.cognitive.microsofttranslator.com/translate"
        params = {
            "api-version": "3.0",
            "from": self.src_lang.lower(),
            "to": self.tgt_lang.lower(),
            "textType": "plain",
        }
        body = [{"text": t} for t in texts]
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Ocp-Apim-Subscription-Region": region,
            "Content-Type": "application/json",
        }
        r = requests.post(endpoint, params=params, json=body, headers=headers, timeout=60)
        r.raise_for_status()
        js = r.json()
        return [item["translations"][0]["text"] for item in js]


def make_translator() -> BaseTranslator:
    provider = (os.getenv("TRANSLATE_PROVIDER") or "deepl").lower()
    api_key = os.getenv("TRANSLATE_API_KEY") or ""
    src = (os.getenv("TRANSLATE_SRC") or DEFAULT_SRC_LANG).upper()
    tgt = (os.getenv("TRANSLATE_TGT") or DEFAULT_TGT_LANG).upper()
    if not api_key:
        raise RuntimeError("TRANSLATE_API_KEY manquant.")

    if provider == "deepl":
        return DeepLTranslator(api_key, src, tgt)
    if provider == "google":
        return GoogleTranslator(api_key, src, tgt)
    if provider == "azure":
        return AzureTranslator(api_key, src, tgt)
    raise RuntimeError(f"Provider inconnu: {provider}")


# ---------- 5eTools helpers ----------
def render_entries_plain(entries) -> str:
    """
    Convertit un champ entries 5eTools (list/str/obj) en texte lisible.
    - Concatène les paragraphes avec lignes vides
    - Laisse les balises {@...} intactes (elles seront protégées ensuite)
    """
    if entries is None:
        return ""
    if isinstance(entries, str):
        return entries
    out = []
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, str):
                out.append(e)
            elif isinstance(e, dict):
                # types communs: {type: "entries", name?, entries:[...]} ou tables/listes
                t = e.get("type")
                if t in ("entries", "list", "inset", "insetReadaloud"):
                    out.append(render_entries_plain(e.get("entries")))
                elif t == "section":
                    title = e.get("name") or ""
                    if title:
                        out.append(f"**{title}**")
                    out.append(render_entries_plain(e.get("entries")))
                else:
                    # fallback brut
                    out.append(json.dumps(e, ensure_ascii=False))
            else:
                out.append(str(e))
    else:
        out.append(str(entries))
    # double retour pour marquer des paragraphes
    return "\n\n".join([normalize_whitespace(x) for x in out if x])


def translate_segments(translator: BaseTranslator, segs: List[str], cache: Dict[str, str]) -> List[str]:
    """Traduit en respectant le cache + balises protégées."""
    results = []
    batch = []
    idxs = []
    # Préparation: protection des tags + normalisation
    prepared = []
    taglists = []
    for i, s in enumerate(segs):
        s = normalize_whitespace(s)
        s_prot, tags = protect_tags(s)
        prepared.append(s_prot)
        taglists.append(tags)

    # Résolution via cache
    for i, s in enumerate(prepared):
        k = cache_key(s, translator.__class__.__name__, translator.src_lang, translator.tgt_lang)
        if k in cache:
            results.append(restore_tags(cache[k], taglists[i]))
        else:
            results.append(None)
            batch.append(s)
            idxs.append(i)

    # Appels API par lots
    for start in range(0, len(batch), BATCH_SIZE):
        part = batch[start:start + BATCH_SIZE]
        try:
            translated = translator.translate_batch(part)
        except Exception as e:
            raise RuntimeError(f"Erreur API: {e}")
        # Mapping retour
        for j, tr in enumerate(translated):
            i_global = idxs[start + j]
            fixed = restore_tags(tr, taglists[i_global])
            results[i_global] = fixed
            # écrire dans cache
            k = cache_key(prepared[i_global], translator.__class__.__name__, translator.src_lang, translator.tgt_lang)
            cache[k] = fixed
        save_cache(cache)
        time.sleep(SLEEP_BETWEEN_CALLS)

    return results


# ---------- Pipeline principal ----------

def fetch_durations(effect):
    if 'duration' in effect:
        if 'seconds' in effect['duration']:
            return f"{effect['duration']['seconds']} seconds"
        elif 'rounds' in effect['duration']:
            return f"{effect['duration']['rounds']} rounds"
        elif 'secounds' in effect['duration'] and 'rounds' in effect['duration']:
            return f"{effect['duration']['seconds']} seconds or {effect['duration']['rounds']} rounds"
        elif 'units' in effect['duration'] and 'value' in effect['duration']:
            return f"{effect['duration']['value']} {effect['duration']['units']}"
    return ""


def fields_to_translate(sp: Dict[str, Any]) -> Dict[str, Any]:
    """Sélectionne les champs source -> clef cible FR."""
    # name -> name_fr
    name_src = sp.get("name") or sp.get("name_en")
    # school peut être code (ex: "EV") — si vous avez déjà un label, remplacez-le ici:
    school_src = sp.get("school_full") or sp.get("school") or ""
    # entries / desc
    if "effects" in sp:
        effects = sp["effects"]
        effects_src = []
        for i, effect in enumerate(effects):
            temp_effect_fr = {'description': render_entries_plain(effect['description']), 'name': effect['name'],
                              'duration': fetch_durations(effect)}
            effects_src.append(temp_effect_fr)

        # effects_src = render_entries_plain(sp["effects"])

    else:
        desc_src = sp.get("desc") or sp.get("entries_fr") or ""
    return {
        "name_fr": name_src or "",
        "school_fr": str(school_src),
        "effects_fr": effects_src or "",
    }


def merge_translations(sp: Dict[str, Any], tr: Dict[str, str]) -> Dict[str, Any]:
    out = dict(sp)
    out["name_fr"] = tr["name_fr"]
    out["school_fr"] = tr["school_fr"]
    out["desc_fr"] = tr["desc_fr"]
    # Option: traduire aussi components/range/time/duration si déjà sous forme lisible
    for k_src, k_dst in [("range", "range_fr"), ("time", "time_fr"), ("components", "components_fr"),
                         ("duration", "duration_fr")]:
        val = sp.get(k_src)
        if isinstance(val, str) and val.strip():
            out[k_dst] = translate_text(val)  # sera défini plus bas (utilise le même traducteur global)
    return out


# traducteur global paresseux, pour traductions ponctuelles simples
_TRANSLATOR = None
_CACHE = None


def translate_text(s: str) -> str:
    global _TRANSLATOR, _CACHE
    if _TRANSLOR := _TRANSLATOR is None:  # avoid typo linters
        pass
    if _TRANSLATOR is None:
        _TRANSLATOR = make_translator()
    if _CACHE is None:
        _CACHE = load_cache()
    res = translate_segments(_TRANSLATOR, [s], _CACHE)[0]
    save_cache(_CACHE)
    return res


def process_file(in_path: str, out_path: str):
    global _TRANSLATOR, _CACHE
    _TRANSLATOR = make_translator()
    _CACHE = load_cache()

    data = json.loads(Path(in_path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Le fichier d'entrée doit être un tableau de sorts.")
    # Construire segments à traduire (pour batcher efficacement)
    todo_keys = ['name', 'type', 'description']  # (index, 'name_fr' / 'school_fr' / 'desc_fr')
    segments = []  # textes
    snapshots = []  # pour reconstruire

    for i, sp in enumerate(data):
        pack = fields_to_translate(sp)
        for key in ("name_fr", "school_fr", "effects_fr"):
            seg = pack[key]
            if seg:
                todo_keys.append((i, key))
                segments.append(seg)
            else:
                todo_keys.append((i, key))
                segments.append("")  # garde l'alignement, sera vide

    # Traduction par lots
    print(f"Segments à traduire: {len(segments)}")
    translated = []
    for start in tqdm(range(0, len(segments), BATCH_SIZE)):
        part = segments[start:start + BATCH_SIZE]
        tr = translate_segments(_TRANSLATOR, part, _CACHE)
        translated.extend(tr)

    # Reconstruction
    out = []
    ptr = 0
    for i, sp in enumerate(data):
        pack = {}
        for key in ("name_fr", "school_fr", "desc_fr"):
            pack[key] = translated[ptr]
            ptr += 1
        out.append(merge_translations(sp, pack))

    Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    save_cache(_CACHE)
    print(f"Écrit: {out_path} ({len(out)} sorts)")


# ---------- CLI ----------
if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Traduire les sorts 5eTools vers le français via API.")
    ap.add_argument("--in", dest="in_path", required=True, help="Fichier JSON d'entrée (5eTools agrégé).")
    ap.add_argument("--out", dest="out_path", required=True, help="Fichier JSON de sortie avec champs *_fr.")
    args = ap.parse_args()
    process_file(args.in_path, args.out_path)
