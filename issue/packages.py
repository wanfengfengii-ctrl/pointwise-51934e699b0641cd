import json
from dataclasses import dataclass

from freezing.history import DAY_ID_PREFIX, FrozenDay
from intake.provenance import require_fields, require_name
from intake.rows import litres_text, require_integer, text_litres

WIRE_VERSION = 1
OFFSET_WIRE_VERSION = 2


@dataclass(frozen=True)
class Package:
    package_id: str
    day_id: str
    kind: str
    snapshot: dict | None = None
    station: str | None = None
    delta: int | None = None
    target_id: str | None = None
    stations: tuple[str, ...] | None = None
    litres: int | None = None

    @property
    def version(self):
        return OFFSET_WIRE_VERSION if self.kind == 'offset' else WIRE_VERSION


def require_operation_id(value):
    value = require_name(value, 'operation id')
    if value.startswith(DAY_ID_PREFIX):
        raise ValueError('operation id uses the reserved day prefix')
    return value


def _require_day_id(value):
    value = require_name(value, 'day id')
    digest = value.removeprefix(DAY_ID_PREFIX)
    if not value.startswith(DAY_ID_PREFIX) or len(digest) != 64:
        raise ValueError('invalid day identity')
    if any(character not in '0123456789abcdef' for character in digest):
        raise ValueError('invalid day identity')
    return value


def make_base_package(day: FrozenDay):
    return Package(day.day_id, day.day_id, 'base', snapshot=day.to_payload())


def make_correction_package(package_id, day_id, station, delta):
    return Package(
        require_operation_id(package_id), _require_day_id(day_id), 'correction',
        station=require_name(station, 'station'), delta=require_integer(delta),
    )


def _require_offset_id(value):
    return require_operation_id(value)


def _normalize_stations(stations):
    if not isinstance(stations, (list, tuple)):
        raise ValueError('offset stations must be a nonempty list or tuple')
    if not stations:
        raise ValueError('offset stations must be a nonempty list or tuple')
    names = tuple(require_name(station, 'station') for station in stations)
    if len(set(names)) != len(names):
        raise ValueError('duplicate offset station')
    return tuple(sorted(names))


def make_offset_package(package_id, day_id, stations, litres):
    package_id = _require_offset_id(package_id)
    day_id = _require_day_id(day_id)
    names = _normalize_stations(stations)
    litres = require_integer(litres)
    if litres <= 0:
        raise ValueError('offset litres must be a positive integer')
    return Package(package_id, day_id, 'offset', stations=names, litres=litres)


def make_withdrawal_package(package_id, correction: Package):
    if correction.kind != 'correction':
        raise ValueError('withdrawal target must be a correction')
    return _withdrawal(
        package_id, correction.day_id, correction.station, -correction.delta, correction.package_id
    )


def _withdrawal(package_id, day_id, station, delta, target_id):
    package_id = require_operation_id(package_id)
    target_id = require_operation_id(target_id)
    if target_id == package_id:
        raise ValueError('a withdrawal cannot target itself')
    return Package(
        package_id, _require_day_id(day_id), 'withdrawal',
        station=require_name(station, 'station'), delta=require_integer(delta), target_id=target_id,
    )


def encode_package(package: Package):
    if package.kind == 'base':
        data = package.snapshot
    elif package.kind in {'correction', 'withdrawal'}:
        data = {'station': package.station, 'delta_litres': litres_text(package.delta)}
        if package.kind == 'withdrawal':
            data['target_id'] = package.target_id
    elif package.kind == 'offset':
        data = {
            'stations': list(package.stations),
            'litres': litres_text(package.litres),
        }
    else:
        raise ValueError('unknown package kind')
    return json.dumps({
        'version': package.version,
        'id': package.package_id,
        'day_id': package.day_id,
        'kind': package.kind,
        'data': data,
    }, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _unique_fields(pairs):
    value = {}
    for key, field in pairs:
        if key in value:
            raise ValueError(f'duplicate JSON field: {key}')
        value[key] = field
    return value


def _invalid_constant(constant):
    raise ValueError(f'invalid JSON constant: {constant}')


def decode_package(wire):
    if not isinstance(wire, str):
        raise ValueError('package must be JSON text')
    try:
        document = json.loads(wire, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)
    except RecursionError as exc:
        raise ValueError('package nesting exceeds JSON decoder capacity') from exc
    require_fields(document, {'version', 'id', 'day_id', 'kind', 'data'}, 'package')
    kind = document['kind']
    if not isinstance(kind, str):
        raise ValueError('unknown package kind')
    expected_version = OFFSET_WIRE_VERSION if kind == 'offset' else WIRE_VERSION
    if type(document['version']) is not int or document['version'] != expected_version:
        raise ValueError('unsupported wire version')
    package_id = require_name(document['id'], 'package id')
    day_id = _require_day_id(document['day_id'])
    data = document['data']
    if kind == 'base':
        if package_id != day_id or not isinstance(data, dict):
            raise ValueError('invalid base package identity or snapshot')
        return Package(package_id, day_id, kind, snapshot=data)
    if kind == 'correction':
        require_fields(data, {'station', 'delta_litres'}, 'correction')
        return make_correction_package(package_id, day_id, data['station'], text_litres(data['delta_litres']))
    if kind == 'withdrawal':
        require_fields(data, {'station', 'delta_litres', 'target_id'}, 'withdrawal')
        return _withdrawal(
            package_id, day_id, data['station'], text_litres(data['delta_litres']), data['target_id']
        )
    if kind == 'offset':
        require_fields(data, {'stations', 'litres'}, 'offset')
        stations = data['stations']
        if not isinstance(stations, list):
            raise ValueError('offset stations must be an array')
        litres = text_litres(data['litres'])
        return make_offset_package(package_id, day_id, stations, litres)
    raise ValueError('unknown package kind')
