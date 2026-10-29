"""Extrait les points d'intérêt de NCPA dans poi/poi.geojson (lancer : python poi/extraire.py).

Sources, sans clé :
- BD TOPO de l'IGN (WFS Géoplateforme) : mairies, offices de tourisme, musées, points de vue, déchèteries.
  Ce sont des zones : on garde le centre de la plus grande, filtrée sur le code INSEE des communes de NCPA.
- Base Mérimée du ministère de la Culture : immeubles protégés au titre des monuments historiques,
  gardés s'ils tombent dans le contour d'une commune de NCPA (le code INSEE de la notice peut être ancien).
  La fiche de chaque monument est complétée par Wikidata (via la référence Mérimée) : photo de Wikimedia
  Commons avec son crédit, et article Wikipédia.

À relancer une fois par an environ pour suivre les nouvelles protections, notices et photos.
"""
import csv
import html
import io
import json
import os
import re
import urllib.parse
import urllib.request

EPCI = "200065563"  # CC Normandie-Cabourg-Pays d'Auge
SORTIE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poi.geojson")
NATURES = {  # nature BD TOPO → catégorie affichée sur la carte
    "Mairie": "mairie",
    "Office de tourisme": "tourisme",
    "Musée": "musee",
    "Point de vue": "point_de_vue",
    "Déchèterie": "decheterie",
}
MERIMEE = "https://ministere-culture.s3.sbg.io.cloud.ovh.net/POP/merimee.csv"
RESUME = 300  # longueur visée du résumé de l'historique, en caractères


def lire(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ncpa3d"}), timeout=300) as r:
        return r.read()


def dans_anneau(x, y, anneau):
    dedans = False
    for (x1, y1), (x2, y2) in zip(anneau, anneau[1:] + anneau[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            dedans = not dedans
    return dedans


def polygones(geometrie):
    return [geometrie["coordinates"]] if geometrie["type"] == "Polygon" else geometrie["coordinates"]


def dans(x, y, geometrie):
    return any(dans_anneau(x, y, p[0]) and not any(dans_anneau(x, y, t) for t in p[1:]) for p in polygones(geometrie))


def centre(geometrie):
    """Centre de gravité de l'anneau extérieur du plus grand polygone (ou le point lui-même)."""
    if geometrie["type"] == "Point":
        return geometrie["coordinates"][:2]
    meilleur = None
    for p in polygones(geometrie):
        a = p[0]
        s = cx = cy = 0.0
        for (x1, y1), (x2, y2) in zip(a, a[1:] + a[:1]):
            k = x1 * y2 - x2 * y1
            s += k
            cx += (x1 + x2) * k
            cy += (y1 + y2) * k
        if s and (meilleur is None or abs(s) > abs(meilleur[0])):
            meilleur = (s, cx / (3 * s), cy / (3 * s))
    if meilleur is None:  # anneau dégénéré : moyenne des sommets
        a = polygones(geometrie)[0][0]
        return [sum(x for x, _ in a) / len(a), sum(y for _, y in a) / len(a)]
    return [meilleur[1], meilleur[2]]


def resume(texte):
    """Premières phrases de l'historique, dans la limite de RESUME caractères."""
    # certaines notices gardent des caractères Windows-1252 (\x92 pour l'apostrophe…)
    texte = re.sub(r"[\x80-\x9f]", lambda m: m.group().encode("latin-1").decode("cp1252", "ignore"), texte)
    texte = " ".join(texte.split()).strip('"« »')
    if not texte:
        return None
    # fin de phrase : ponctuation après une minuscule ou un chiffre (pas après « E. » d'un prénom)
    phrases = re.split(r"(?<=[a-zàâçéèêëîïôûùüÿœ0-9)»][.!?])\s+", texte)
    garde = phrases[0]
    for p in phrases[1:]:
        if len(garde) + 1 + len(p) > RESUME:
            break
        garde += " " + p
    if len(garde) > RESUME:
        garde = garde[:RESUME].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    elif garde != texte:
        garde += " …"
    return garde


def wikidata(monuments):
    """Photo Commons (avec auteur et licence) et article Wikipédia des monuments, par leur référence Mérimée."""
    refs = " ".join(f'"{f["properties"]["id"]}"' for f in monuments)
    rep = json.loads(lire("https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"format": "json", "query": f"""
        SELECT ?ref ?image ?article WHERE {{
          VALUES ?ref {{ {refs} }} ?item wdt:P380 ?ref .
          OPTIONAL {{ ?item wdt:P18 ?image }}
          OPTIONAL {{ ?article schema:about ?item ; schema:isPartOf <https://fr.wikipedia.org/> }}
        }}"""})))
    infos = {}
    for b in rep["results"]["bindings"]:  # une seule photo par monument, même s'il en a plusieurs
        infos.setdefault(b["ref"]["value"], {k: v["value"] for k, v in b.items()})
    fichiers = {"File:" + urllib.parse.unquote(i["image"].rsplit("/", 1)[1]): r for r, i in infos.items() if "image" in i}
    photos = {}
    noms = list(fichiers)
    for i in range(0, len(noms), 50):
        rep = json.loads(lire("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "format": "json", "prop": "imageinfo", "iiprop": "url|extmetadata", "iiurlwidth": 500,
            "iiextmetadatafilter": "Artist|LicenseShortName", "titles": "|".join(noms[i:i + 50])})))
        norm = {n["to"]: n["from"] for n in rep["query"].get("normalized", [])}
        for page in rep["query"]["pages"].values():
            if "imageinfo" not in page:
                continue
            ii = page["imageinfo"][0]
            meta = {k: v["value"] for k, v in ii.get("extmetadata", {}).items()}
            auteur = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", meta.get("Artist", ""))).split())
            auteur = re.sub(r"\s*\d{1,2}:\d{2}, \d{1,2} \w+ \d{4} \(UTC\)", "", auteur)  # signature horodatée
            auteur = "" if "unknown author" in auteur.lower() else auteur
            photos[fichiers[norm.get(page["title"], page["title"])]] = {
                "photo": ii["thumburl"].split("?")[0], "photo_page": ii["descriptionurl"],
                "photo_credit": " · ".join(v for v in (auteur, meta.get("LicenseShortName")) if v)}
    for f in monuments:
        i = infos.get(f["properties"]["id"], {})
        f["properties"].update({k: v for k, v in photos.get(f["properties"]["id"], {}).items() if v})
        if "article" in i:
            f["properties"]["wikipedia"] = i["article"]
    print(f"Wikidata : {len(photos)} photos, {sum('article' in i for i in infos.values())} articles Wikipédia")


def point(x, y, proprietes):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(x, 6), round(y, 6)]},
            "properties": {k: v for k, v in proprietes.items() if v not in (None, "")}}


def main():
    communes = json.loads(lire("https://geo.api.gouv.fr/communes?" + urllib.parse.urlencode(
        {"codeEpci": EPCI, "fields": "nom,code,contour", "format": "geojson", "geometry": "contour"})))["features"]
    noms = {c["properties"]["code"]: c["properties"]["nom"] for c in communes}
    xs = [x for c in communes for p in polygones(c["geometry"]) for x, _ in p[0]]
    ys = [y for c in communes for p in polygones(c["geometry"]) for _, y in p[0]]
    print(f"{len(communes)} communes")

    points = []
    natures = ",".join("'" + n.replace("'", "''") + "'" for n in NATURES)
    wfs = json.loads(lire("https://data.geopf.fr/wfs/ows?" + urllib.parse.urlencode({
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "OUTPUTFORMAT": "application/json",
        "TYPENAMES": "BDTOPO_V3:zone_d_activite_ou_d_interet", "COUNT": 5000,
        "CQL_FILTER": f"nature IN ({natures}) AND BBOX(geometrie,{min(xs)},{min(ys)},{max(xs)},{max(ys)},'EPSG:4326')"})))
    for f in wfs["features"]:
        p = f["properties"]
        if p.get("insee_commune") not in noms or p.get("etat_de_l_objet") not in (None, "En service"):
            continue
        x, y = centre(f["geometry"])
        points.append(point(x, y, {
            "categorie": NATURES[p["nature"]], "nom": p.get("toponyme") or p["nature"],
            "commune": noms[p["insee_commune"]], "adresse": (p.get("adresse_postale") or "").capitalize(),
            "source": "BD TOPO IGN", "id": p["cleabs"]}))

    csv.field_size_limit(10 ** 9)
    texte = io.StringIO(lire(MERIMEE).decode("utf-8-sig"))
    for r in csv.DictReader(texte, delimiter="|"):
        if r["Departement_format_numerique"] != "14" or not r["Reference"].startswith("PA"):
            continue
        try:
            y, x = (float(v) for v in r["coordonnees_au_format_WGS84"].split(","))
        except ValueError:
            continue
        commune = next((c for c in communes if dans(x, y, c["geometry"])), None)
        if not commune:
            continue
        typo = r["Typologie_de_la_protection"].lower()
        points.append(point(x, y, {
            "categorie": "monument", "nom": r["Titre_editorial_de_la_notice"] or r["Denomination_de_l_edifice"],
            "commune": commune["properties"]["nom"],
            "adresse": r["Adresse_forme_editoriale"],
            "protection": "Classé" if "classé" in typo else "Inscrit" if "inscrit" in typo else None,
            "protections": r["Date_et_typologie_de_la_protection"],
            "siecle": r["Format_abrege_du_siecle_de_construction"].replace(";", ", "),
            "datation": r["Datation_de_l_edifice"].replace(";", ", "),
            "auteurs": r["Auteur_de_l_edifice"].replace(";", ", "),
            "historique": resume(r["Historique"]),
            "statut": r["Statut_juridique_de_l_edifice"],
            "source": "Mérimée, ministère de la Culture", "id": r["Reference"]}))

    wikidata([f for f in points if f["properties"]["categorie"] == "monument"])

    # altitude du sol (RGE ALTI, m NGF, comme le relief de la carte) en 3e coordonnée : épingles et
    # groupes d'épingles sont ainsi placés au sol même sans relief affiché
    for i in range(0, len(points), 50):
        lot = points[i:i + 50]
        rep = json.loads(lire("https://data.geopf.fr/altimetrie/1.0/calcul/alti/rest/elevation.json?" + urllib.parse.urlencode({
            "lon": "|".join(str(f["geometry"]["coordinates"][0]) for f in lot),
            "lat": "|".join(str(f["geometry"]["coordinates"][1]) for f in lot),
            "resource": "ign_rge_alti_wld", "zonly": "true"})))
        for f, z in zip(lot, rep["elevations"]):
            if z > -1000:  # -99999 : pas de donnée
                f["geometry"]["coordinates"].append(round(z, 1))

    points.sort(key=lambda f: (f["properties"]["categorie"], f["properties"]["commune"], f["properties"]["nom"]))
    with open(SORTIE, "w", encoding="utf-8") as s:
        s.write('{"type":"FeatureCollection","features":[\n')
        s.write(",\n".join(json.dumps(f, ensure_ascii=False, separators=(",", ":")) for f in points))
        s.write("\n]}\n")
    compte = {}
    for f in points:
        compte[f["properties"]["categorie"]] = compte.get(f["properties"]["categorie"], 0) + 1
    print(compte, "→", SORTIE)


if __name__ == "__main__":
    main()
