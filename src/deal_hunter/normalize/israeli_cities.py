"""Normalize Hebrew municipality names for cross-source filtering (OnMap, ad.co.il, etc.)."""


def hebrew_city_norm(s: str) -> str:
    if not s:
        return ""
    t = s.replace("-", " ").replace("־", " ")
    return " ".join(t.split())


_HEBREW_CITY_SYNONYMS: dict[str, str] = {}


def _register_city_group(canonical: str, *aliases: str) -> None:
    ck = hebrew_city_norm(canonical)
    for a in (canonical,) + aliases:
        ak = hebrew_city_norm(a)
        if ak:
            _HEBREW_CITY_SYNONYMS[ak] = ck


_register_city_group("תל אביב יפו", "תל אביב-יפו", "תל אביב")
_register_city_group("קריית אונו", "קרית אונו")
_register_city_group("קריית גת", "קרית גת")
_register_city_group("קריית ביאליק", "קרית ביאליק")
_register_city_group("קריית מוצקין", "קרית מוצקין")
_register_city_group("קריית ים", "קרית ים")
_register_city_group("קריית אתא", "קרית אתא")
_register_city_group("פתח תקווה", "פתח תקוה")
_register_city_group("ראשון לציון", 'ראשל"צ', "ראשל״צ")
_register_city_group("באר שבע", "באר-שבע")
_register_city_group("נוף הגליל", "נוף הגליל עילית")


def hebrew_city_match_key(name: str) -> str:
    k = hebrew_city_norm(name)
    return _HEBREW_CITY_SYNONYMS.get(k, k)


def hebrew_allowed_city_keys(names: list[str]) -> set[str]:
    return {hebrew_city_match_key(c) for c in names if c}
