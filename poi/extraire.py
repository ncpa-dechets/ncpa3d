"""Extrait les points d'intérêt de NCPA dans poi/poi.geojson (lancer : python poi/extraire.py).

Sources, sans clé :
- BD TOPO de l'IGN (WFS Géoplateforme) : mairies, offices de tourisme, musées, points de vue, déchèteries.
  Ce sont des zones : on garde le centre de la plus grande, filtrée sur le code INSEE des communes de NCPA.
- Base Mérimée du ministère de la Culture : immeubles protégés au titre des monuments historiques,
  gardés s'ils tombent dans le contour d'une commune de NCPA (le code INSEE de la notice peut être ancien).
"""
import csv
import io
import json
import os
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
            "statut": r["Statut_juridique_de_l_edifice"],
            "source": "Mérimée, ministère de la Culture", "id": r["Reference"]}))

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
