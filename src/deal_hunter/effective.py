from __future__ import annotations

from typing import Any


def effective_sqm(obj: dict[str, Any] | Any) -> int | None:
    sqm = getattr(obj, "sqm", None) if not isinstance(obj, dict) else obj.get("sqm")
    sqm_u = (
        getattr(obj, "sqm_user", None)
        if not isinstance(obj, dict)
        else obj.get("sqm_user")
    )
    return sqm_u if sqm_u is not None else sqm


def effective_sqm_build(obj: dict[str, Any] | Any) -> int | None:
    b = (
        getattr(obj, "sqm_build", None)
        if not isinstance(obj, dict)
        else obj.get("sqm_build")
    )
    b_u = (
        getattr(obj, "sqm_build_user", None)
        if not isinstance(obj, dict)
        else obj.get("sqm_build_user")
    )
    return b_u if b_u is not None else b


def effective_units(obj: dict[str, Any] | Any) -> int | None:
    u = (
        getattr(obj, "units_count", None)
        if not isinstance(obj, dict)
        else obj.get("units_count")
    )
    u_u = (
        getattr(obj, "units_count_user", None)
        if not isinstance(obj, dict)
        else obj.get("units_count_user")
    )
    return u_u if u_u is not None else u


def effective_garden_sqm(obj: dict[str, Any] | Any) -> int | None:
    g = (
        getattr(obj, "garden_sqm", None)
        if not isinstance(obj, dict)
        else obj.get("garden_sqm")
    )
    g_u = (
        getattr(obj, "garden_sqm_user", None)
        if not isinstance(obj, dict)
        else obj.get("garden_sqm_user")
    )
    return g_u if g_u is not None else g


def effective_rooms(obj: dict[str, Any] | Any) -> float | None:
    r = (
        getattr(obj, "rooms", None)
        if not isinstance(obj, dict)
        else obj.get("rooms")
    )
    r_u = (
        getattr(obj, "rooms_user", None)
        if not isinstance(obj, dict)
        else obj.get("rooms_user")
    )
    return r_u if r_u is not None else r


def effective_price_per_sqm(obj: dict[str, Any] | Any) -> int | None:
    sqm = effective_sqm(obj)
    price = (
        getattr(obj, "price", None)
        if not isinstance(obj, dict)
        else obj.get("price")
    )
    if sqm and price and sqm > 0:
        return round(price / sqm)
    stored = (
        getattr(obj, "price_per_sqm", None)
        if not isinstance(obj, dict)
        else obj.get("price_per_sqm")
    )
    return stored
