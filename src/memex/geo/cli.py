"""CLI `memex-geo` — usar la capa de mapas determinista desde la terminal.

Subcomandos:
  geocode  — dirección/lugar → coordenadas (con cualquiera de los proveedores).
  trip     — tiempo y distancia estimados de un viaje A→B, partiendo de un punto X
             explícito (`--from-point`, ejercita el seam `LocationSource`) o de una
             dirección de origen (`--from`), con `--depart` opcional para una hora futura.

Server-side: habla con el proveedor vía httpx. La key sale de una env var (Doppler),
seleccionada por proveedor; correr con `doppler run -- memex-geo ...`.

Exit code 0 si OK; 1 si error del proveedor/config; 2 si argumentos inválidos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime

from dotenv import load_dotenv

from memex.geo import (
    GeocodeResult,
    GeoConfigError,
    GeoError,
    GeoNotFoundError,
    GeoPoint,
    ManualLocationSource,
    ResolvedPlace,
    TravelEstimate,
    TravelMode,
    build_provider_from_env,
    estimate_trip,
    estimate_trip_from_source,
    geocode_address,
    known_providers,
    resolve_place,
    reverse_geocode,
)
from memex.logging import get_logger, setup_logging


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _parse_point(value: str | None) -> GeoPoint | None:
    """Parsea `"lat,lng"` → GeoPoint, o None si no matchea ese formato."""
    if not value:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        return GeoPoint(float(parts[0].strip()), float(parts[1].strip()))
    except ValueError:
        return None


def _fmt_distance(meters: int) -> str:
    return f"{meters / 1000:.1f} km" if meters >= 1000 else f"{meters} m"


def _fmt_duration(seconds: int) -> str:
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min" if minutes else f"{seconds} s"


def _build_parser() -> argparse.ArgumentParser:
    # Args compartidos en un parent parser → se aceptan DESPUÉS del subcomando
    # (`memex-geo geocode --provider ors`), que es la posición natural.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--provider",
        choices=known_providers(),
        default=None,
        help="Proveedor de mapas (default: MEMEX_GEO_PROVIDER o 'google').",
    )
    common.add_argument("--json", action="store_true", help="Salida JSON (machine-readable).")

    parser = argparse.ArgumentParser(prog="memex-geo")
    sub = parser.add_subparsers(dest="cmd", required=True)

    geo_p = sub.add_parser("geocode", parents=[common], help="Dirección/lugar → coordenadas.")
    geo_p.add_argument("--address", required=True, help="Dirección o lugar a geocodificar.")

    trip_p = sub.add_parser(
        "trip",
        parents=[common],
        help="Tiempo y distancia estimados de un viaje A→B.",
        epilog="Una coordenada que empieza con '-' (lat/long negativa) se pasa con '=': "
        "--from-point=-34.6,-58.4 o --to=-34.8,-58.5.",
    )
    trip_p.add_argument("--from", dest="from_addr", help="Origen (dirección o 'lat,lng').")
    trip_p.add_argument(
        "--from-point",
        dest="from_point",
        help="Punto X de referencia 'lat,lng' (gana sobre --from; ejercita LocationSource).",
    )
    trip_p.add_argument(
        "--to", dest="to_addr", required=True, help="Destino (dirección o 'lat,lng')."
    )
    trip_p.add_argument(
        "--mode",
        choices=[m.value for m in TravelMode],
        default=TravelMode.DRIVING.value,
        help="Modo de viaje (default driving).",
    )
    trip_p.add_argument(
        "--depart",
        default=None,
        help="Hora de salida ISO 8601 con zona (ej. 2026-06-03T08:00:00-05:00) para tráfico.",
    )

    place_p = sub.add_parser(
        "place",
        parents=[common],
        help="Coordenadas → dirección + nombre de lugar (POI).",
        epilog="Una coordenada que empieza con '-' se pasa con '=': --point=-34.6,-58.4.",
    )
    place_p.add_argument(
        "--point", required=True, help="Punto 'lat,lng' a resolver (ej. 4.65,-74.05)."
    )
    place_p.add_argument(
        "--reverse-only",
        action="store_true",
        help="Solo dirección (reverse geocoding), sin buscar el nombre del negocio/POI.",
    )
    place_p.add_argument(
        "--radius",
        type=float,
        default=50.0,
        help="Radio en metros para buscar el POI más cercano (default 50).",
    )
    return parser


def _print_geocode(result: GeocodeResult, *, as_json: bool) -> None:
    if as_json:
        _say(
            json.dumps(
                {
                    "lat": result.point.lat,
                    "lng": result.point.lng,
                    "formatted_address": result.formatted_address,
                    "place_id": result.provider_place_id,
                    "confidence": result.confidence,
                }
            )
        )
        return
    _say(result.formatted_address)
    _say(f"  {result.point.lat:.6f}, {result.point.lng:.6f}")
    if result.confidence is not None:
        _say(f"  confianza: {result.confidence}")
    if result.provider_place_id:
        _say(f"  place_id: {result.provider_place_id}")


def _print_trip(estimate: TravelEstimate, *, departure_requested: bool, as_json: bool) -> None:
    if as_json:
        _say(
            json.dumps(
                {
                    "duration_s": estimate.duration_s,
                    "distance_m": estimate.distance_m,
                    "duration_in_traffic_s": estimate.duration_in_traffic_s,
                    "mode": estimate.mode.value,
                }
            )
        )
        return
    _say(f"Distancia: {_fmt_distance(estimate.distance_m)}")
    _say(f"Duración:  {_fmt_duration(estimate.duration_s)}")
    if estimate.duration_in_traffic_s is not None:
        _say(f"Con tráfico: {_fmt_duration(estimate.duration_in_traffic_s)}")
    elif departure_requested:
        _say("(el proveedor no devolvió estimación con tráfico para esa hora)")


def _print_place(place: ResolvedPlace, *, as_json: bool) -> None:
    if as_json:
        _say(
            json.dumps(
                {
                    "name": place.name,
                    "formatted_address": place.formatted_address,
                    "lat": place.point.lat,
                    "lng": place.point.lng,
                    "place_id": place.provider_place_id,
                    "types": list(place.types),
                    "in_transit": place.in_transit,
                }
            )
        )
        return
    if place.name:
        _say(place.name)
    if place.formatted_address:
        _say(f"  {place.formatted_address}")
    if place.types:
        _say(f"  tipos: {', '.join(place.types)}")
    if not place.name and not place.formatted_address:
        _say("(sin resultado)")


async def _cmd_geocode(args: argparse.Namespace) -> int:
    provider = build_provider_from_env(provider=args.provider)
    try:
        result = await geocode_address(provider, args.address)
    finally:
        await provider.aclose()
    _print_geocode(result, as_json=args.json)
    return 0


async def _cmd_trip(args: argparse.Namespace) -> int:
    mode = TravelMode(args.mode)

    departure_time: datetime | None = None
    if args.depart:
        try:
            departure_time = datetime.fromisoformat(args.depart)
        except ValueError:
            _say(f"--depart inválido: {args.depart!r} (usá ISO 8601 con zona).", err=True)
            return 2
        if departure_time.tzinfo is None:
            _say("Aviso: --depart sin zona horaria; se interpreta en hora local.", err=True)

    if not args.from_addr and not args.from_point:
        _say("Indicá el origen con --from o --from-point.", err=True)
        return 2
    from_point = _parse_point(args.from_point) if args.from_point else None
    if args.from_point and from_point is None:
        _say("--from-point debe ser 'lat,lng' (ej. -34.60,-58.38).", err=True)
        return 2

    provider = build_provider_from_env(provider=args.provider)
    try:
        destination = (
            _parse_point(args.to_addr) or (await geocode_address(provider, args.to_addr)).point
        )
        if from_point is not None:
            estimate = await estimate_trip_from_source(
                provider,
                ManualLocationSource(from_point),
                destination,
                mode=mode,
                departure_time=departure_time,
            )
        else:
            origin = (
                _parse_point(args.from_addr)
                or (await geocode_address(provider, args.from_addr)).point
            )
            estimate = await estimate_trip(
                provider,
                origin=origin,
                destination=destination,
                mode=mode,
                departure_time=departure_time,
            )
    finally:
        await provider.aclose()

    _print_trip(estimate, departure_requested=args.depart is not None, as_json=args.json)
    return 0


async def _cmd_place(args: argparse.Namespace) -> int:
    point = _parse_point(args.point)
    if point is None:
        _say("--point debe ser 'lat,lng' (ej. 4.65,-74.05).", err=True)
        return 2
    provider = build_provider_from_env(provider=args.provider)
    try:
        if args.reverse_only:
            rg = await reverse_geocode(provider, point)
            place = ResolvedPlace(
                formatted_address=rg.formatted_address,
                point=point,
                provider_place_id=rg.provider_place_id,
            )
        else:
            place = await resolve_place(provider, point, radius_m=args.radius)
    finally:
        await provider.aclose()
    _print_place(place, as_json=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.geo.cli")
    args = _build_parser().parse_args(argv)

    try:
        if args.cmd == "geocode":
            return asyncio.run(_cmd_geocode(args))
        if args.cmd == "trip":
            return asyncio.run(_cmd_trip(args))
        if args.cmd == "place":
            return asyncio.run(_cmd_place(args))
    except GeoNotFoundError as e:
        _say(f"Sin resultados para {e.query!r}.", err=True)
        return 1
    except GeoConfigError as e:
        _say(
            f"Config inválida: {e}. ¿Corriste con `doppler run -- memex-geo ...`? "
            "¿Está seteada GMAPS_API_KEY / OPENROUTE_API_KEY?",
            err=True,
        )
        return 1
    except GeoError as e:
        _say(f"Error del proveedor de mapas: {e}", err=True)
        log.warning("geo.cli.error", error=str(e))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
