"""Bounded editorial evidence ZIP. Native Claude Sonnet 5 draft, root revised.

No disk/network/hardware access. Existing pathexport owns trace interpolation.
"""
import copy
import hashlib
import io
import json
import math
import zipfile
from . import pathexport

MAX_BYTES = 16 * 1024 * 1024
README = '''OsmoDesk / editorial evidence

Measured, decimated pan/tilt telemetry is on the programmed-motion clock;
cue waits are excluded. Resampled frames are NOT video timecode or frame sync.
CSV/CHAN rotations are relative to the first measured sample, in degrees, ZXY.
Zero translation/roll channels mean unmeasured, not proof of a stationary rig.
No lens calibration or field of view is encoded. Angular motion cannot prove a
composite will match. Recording requested is NOT recording confirmed.
Aborted runs, telemetry gaps and incomplete coverage remain in evidence.json.
Manual notes without a trace contain no measured-path export.
path.json is the programmed path saved WITH THAT TAKE, never the current draft.
manifest.json hashes all other files; hashes detect changes, not authenticity.
'''


def _json(value):
    def check(v, depth=0):
        if depth > 20:
            raise ValueError('Evidence nesting exceeds export limits')
        if isinstance(v, str) and len(v) > 4096:
            raise ValueError('Evidence text exceeds export limits')
        if isinstance(v, dict):
            for k, item in v.items():
                if not isinstance(k, str):
                    raise ValueError('Invalid evidence field')
                check(item, depth + 1)
        elif isinstance(v, (tuple, list)):
            if len(v) > 2000:
                raise ValueError('Evidence collection exceeds export limits')
            for item in v:
                check(item, depth + 1)
    check(value)
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Evidence is not valid finite JSON') from None
    if len(raw) > MAX_BYTES:
        raise ValueError('Evidence exceeds export size limits')
    return raw


def _path(path, include_notes):
    if path is None:
        return None
    if not isinstance(path, dict):
        raise ValueError('Saved take path is invalid')
    # A canonical Move can have freeform setup notes. Never export those as a
    # side effect of a default anonymised handoff, even nested under the path.
    from .moves import Move
    out = Move.from_dict(path).to_dict()
    out['setup'] = {k: v for k, v in out.get('setup', {}).items()
                    if k in ('fps', 'resolution', 'color', 'iso', 'shutter', 'wb', 'fov_deg')}
    if not include_notes:
        out['name'] = 'Saved shot'
        for index, waypoint in enumerate(out['waypoints']):
            waypoint['name'] = 'P' + str(index + 1)
    return out


def build_package(takes, ids, *, fps=25, include_notes=False):
    if (not isinstance(takes, list) or len(takes) > 2000 or not isinstance(ids, list)
            or not 1 <= len(ids) <= 20 or any(type(i) is not int or i < 0 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('Select between 1 and 20 distinct takes')
    if type(include_notes) is not bool:
        raise ValueError('Include labels and notes must be a boolean')
    if type(fps) not in (int, float) or not 1 <= fps <= 120:
        raise ValueError('Export frame rate must be between 1 and 120')
    files = {'README.txt': README.encode('utf-8')}
    def add(name, data):
        if sum(map(len, files.values())) + len(data) > MAX_BYTES:
            raise ValueError('Select fewer takes; package exceeds 16 MB')
        files[name] = data
    for ident in ids:
        selected = [t for t in takes if isinstance(t, dict) and type(t.get('id')) is int and t['id'] == ident]
        if len(selected) != 1:
            raise ValueError('A selected take is missing or ambiguous')
        take = selected[0]
        evidence = {k: copy.deepcopy(take[k]) for k in
                    ('id', 'take', 'circled', 'run_id', 'fingerprint', 'duration') if k in take}
        evidence['source'] = 'shot run' if take.get('run_id') else 'manual note; no run attached'
        record = take.get('recording')
        evidence['recording'] = ({k: record[k] for k in ('requested', 'reported') if type(record.get(k)) is bool}
                                 if isinstance(record, dict) else None)
        if include_notes:
            evidence.update({k: take[k] for k in ('scene', 'shot', 'note', 'move') if k in take})
        setup = take.get('setup_fields', {})
        evidence['setup_fields'] = {k: setup[k] for k in ('fps', 'resolution', 'color', 'iso', 'shutter', 'wb', 'fov_deg') if k in setup}
        motion = take.get('motion')
        if motion is not None and not isinstance(motion, dict):
            raise ValueError('Invalid take motion evidence')
        evidence['motion'] = ({k: copy.deepcopy(motion[k]) for k in
            ('verdict', 'repeatable', 'peak_error', 'peak_error_pitch', 'peak_error_yaw',
             'clamp_events', 'telemetry_gaps', 'cues_waited', 'samples', 'elapsed', 'aborted', 'trace') if k in motion}
            if motion is not None else None)
        trace = motion.get('trace') if motion else None
        if trace is not None and not isinstance(trace, list):
            raise ValueError('Invalid measured trace')
        measured = bool(trace)
        evidence['measured'] = measured
        prefix = f'take_{ident}/'
        if measured:
            if not 2 <= len(trace) <= 300:
                raise ValueError('Measured export needs 2 to 300 samples')
            for i, point in enumerate(trace):
                if (not isinstance(point, (tuple, list)) or len(point) != 3
                        or any(type(n) not in (int, float) or not math.isfinite(n) for n in point)
                        or (i and point[0] <= trace[i-1][0])):
                    raise ValueError('Measured trace must be finite and strictly time ordered')
            span = trace[-1][0] - trace[0][0]
            if not 0 < span <= 3600 or round(span * fps) + 1 > 100000:
                raise ValueError('Measured trace exceeds frame export limits')
            add(prefix + 'measured-path.csv', pathexport.to_csv(trace, fps=fps).encode())
            add(prefix + 'measured-path.chan', pathexport.to_chan(trace, fps=fps).encode())
        add(prefix + 'evidence.json', _json(evidence))
        path = _path(take.get('path'), include_notes)
        if path is not None:
            add(prefix + 'path.json', _json(path))
    manifest = {'version': 1, 'selected': ids, 'fps': fps, 'include_notes': include_notes,
                'files': [{'file': name, 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in files.items()]}
    add('manifest.json', _json(manifest))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    if len(output.getvalue()) > MAX_BYTES:
        raise ValueError('Package exceeds 16 MB')
    return output.getvalue()
