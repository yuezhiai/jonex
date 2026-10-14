"""Lossless, checksummed JSON partitions inside one immutable publication.

Readers restore the original dictionaries/lists; only the physical layout
changes. References are always relative to the pinned version, never another
version or the live published pointer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from collections import OrderedDict
from collections.abc import Mapping
from threading import RLock

from jonex_wiki.kernel.telemetry import count_work

MARKER = "_jonex_json_shards"


def _list_entries(value, id_field):
    occurrences = {}
    for item in value:
        if callable(id_field):
            key = id_field(item)
            occurrence = occurrences.get(key,0);occurrences[key]=occurrence+1
            key = key+':'+str(occurrence)
        else:
            key = item[id_field] if isinstance(item,dict) else getattr(item,id_field)
        yield key,item


def _groups(value, id_field=None, owners=None, bucket_count=256):
    if id_field is None:
        entries = value.items()
    else:
        entries = _list_entries(value,id_field)
    result = {}
    for key, item in entries:
        if not isinstance(key, str):
            raise ValueError("JSON partition keys must be strings")
        owner = (owners or {}).get(key)
        group = ("doc-" + hashlib.sha256(owner.encode()).hexdigest()[:32] if owner
                 else "bucket-" + format(int(hashlib.sha256(key.encode()).hexdigest()[:2],16) % bucket_count,'02x'))
        part = result.setdefault(group, {})
        if key in part:
            raise ValueError("duplicate JSON partition record: " + key)
        part[key] = item
    return result


def _equal(current, previous):
    for key, value in current.items():
        if key not in previous:
            return False
        old = previous[key]
        if hasattr(value, "model_dump") and not hasattr(old, "model_dump"):
            value = value.model_dump()
        elif hasattr(old, "model_dump") and not hasattr(value, "model_dump"):
            old = old.model_dump()
        if value != old:
            return False
    return True


def iter_json_artifact(path, data, *, fields, encode, previous=None,
                       previous_specs=None, file_references=None, owners=None, bucket_count=256):
    """Yield changed partitions and early references, then their small manifest.

    ``fields`` maps a dictionary field to None, or a list field to its unique
    record-id attribute/callable. Callable identities retain duplicate entries
    using an occurrence suffix. List order is retained in the manifest.
    """
    if MARKER in data:
        raise ValueError("reserved JSON partition marker")
    header = {key: value for key, value in data.items() if key not in fields}
    specs = {}
    for field, id_field in fields.items():
        value = data[field]
        groups = _groups(value, id_field, (owners or {}).get(field), bucket_count)
        old_entries = (previous or {}).get(field, [] if id_field else {})
        if id_field:
            old_entries = dict(_list_entries(old_entries,id_field))
        old_spec = (previous_specs or {}).get(field, {})
        old_parts = {part["path"]: part for part in old_spec.get("parts", [])}
        parts = []
        for group, entries in sorted(groups.items()):
            part_path = f"json-shards/{path.removesuffix('.json')}/{field}/{group}.json"
            keys_sha256 = hashlib.sha256(encode(sorted(entries))).hexdigest()
            old_part = old_parts.get(part_path)
            reference = (file_references or {}).get(part_path)
            if (old_part is not None and reference is not None
                    and reference.sha256 == old_part["sha256"]
                    and old_part.get("count") == len(entries)
                    and old_part.get("keys_sha256") == keys_sha256
                    # The stored member digest/count above bind this exact
                    # subset; no second grouping of the old full map is needed.
                    and _equal(entries, old_entries)):
                checksum = reference.sha256
                content = reference
                count_work("json_shards_reused_early")
            else:
                content = encode(entries)
                checksum = hashlib.sha256(content).hexdigest()
                count_work("json_shards_encoded")
                count_work("json_shard_bytes_encoded", len(content))
            parts.append({"path": part_path, "sha256": checksum,
                          "count": len(entries), "keys_sha256": keys_sha256, 'keys':sorted(entries)})
            yield part_path, content
        spec = {"kind": "list" if id_field else "dict", "parts": parts}
        if id_field:
            spec["order"] = [key for key,item in _list_entries(value,id_field)]
        specs[field] = spec
    header[MARKER] = {"version": 2, "fields": specs}
    yield path, encode(header)


def _part_values(read_bytes, part, content=None):
    content = read_bytes(part['path']) if content is None else content
    if hashlib.sha256(content).hexdigest() != part.get('sha256'):
        raise ValueError('JSON partition checksum mismatch: '+part['path'])
    entries = json.loads(content)
    if not isinstance(entries,dict) or len(entries) != part['count']:
        raise ValueError('JSON partition count mismatch')
    keys = sorted(entries)
    encoded = json.dumps(keys,ensure_ascii=False,separators=(',',':')).encode()
    if hashlib.sha256(encoded).hexdigest() != part.get('keys_sha256'):
        raise ValueError('JSON partition member mismatch')
    if 'keys' in part and keys != part['keys']:
        raise ValueError('JSON partition key catalog mismatch')
    return entries


class PartitionedMapping(Mapping):
    """One revision-pinned field, with bounded selected-part caching."""
    def __init__(self, read_bytes, parts):
        self.read_bytes=read_bytes;self.parts=parts;self.keys_to_part={}
        self.cache=OrderedDict();self.lock=RLock()
        self.cache_budget=8*1024*1024;self.cache_bytes=0
        for index,part in enumerate(parts):
            keys=part.get('keys')
            if (not isinstance(keys,list) or not all(isinstance(k,str) for k in keys)
                    or keys != sorted(set(keys)) or len(keys) != part['count']):
                raise ValueError('invalid JSON partition key catalog')
            encoded=json.dumps(keys,ensure_ascii=False,separators=(',',':')).encode()
            if hashlib.sha256(encoded).hexdigest()!=part['keys_sha256']:
                raise ValueError('JSON partition member mismatch')
            for key in keys:
                if key in self.keys_to_part:raise ValueError('duplicate JSON partition record')
                self.keys_to_part[key]=index

    def __iter__(self):return iter(self.keys_to_part)
    def __len__(self):return len(self.keys_to_part)
    def __contains__(self,key):return key in self.keys_to_part
    def __getitem__(self,key):
        index=self.keys_to_part[key]
        with self.lock:
            if index not in self.cache:
                content=self.read_bytes(self.parts[index]['path'])
                values=_part_values(self.read_bytes,self.parts[index],content)
                if len(content)>self.cache_budget:
                    count_work('json_shards_loaded_on_demand')
                    return values[key]
                self.cache[index]=(values,len(content));self.cache_bytes+=len(content)
                count_work('json_shards_loaded_on_demand')
                while len(self.cache)>1 and (len(self.cache)>32 or self.cache_bytes>self.cache_budget):
                    old,size=self.cache.popitem(last=False)[1];self.cache_bytes-=size
            self.cache.move_to_end(index)
            return self.cache[index][0][key]


def load_json_artifact(read_bytes, path, *, manifests=None, lazy_fields=()):
    """Restore old monolithic or new partitioned JSON, rejecting broken refs."""
    data = json.loads(read_bytes(path))
    if not isinstance(data, dict) or MARKER not in data:
        return data
    marker = data.pop(MARKER)
    if (not isinstance(marker, dict) or type(marker.get("version")) is not int or marker.get("version") not in (1,2)
            or not isinstance(marker.get("fields"), dict)):
        raise ValueError("unsupported JSON partition manifest")
    prefix = f"json-shards/{path.removesuffix('.json')}/"
    seen_paths = set()
    for field, spec in marker["fields"].items():
        if (not isinstance(field, str) or field in data or not isinstance(spec, dict)
                or spec.get("kind") not in ("dict", "list")):
            raise ValueError("invalid JSON partition field")
        values = {}
        if not isinstance(spec.get("parts"), list):
            raise ValueError("invalid JSON partition list")
        for part in spec["parts"]:
            if not isinstance(part, dict) or type(part.get("count")) is not int or part["count"] < 0:
                raise ValueError("invalid JSON partition descriptor")
            if marker['version']==2 and 'keys' not in part:
                raise ValueError('missing JSON partition key catalog')
            relative = part.get("path", "")
            if not isinstance(relative, str):
                raise ValueError("invalid JSON partition path")
            pure = PurePosixPath(relative)
            if (not relative.startswith(prefix + field + "/") or pure.is_absolute()
                    or any(segment in ("", ".", "..") for segment in relative.split("/"))
                    or "\\" in relative or relative in seen_paths):
                raise ValueError("invalid JSON partition path")
            seen_paths.add(relative)
        if (marker['version']==2 and spec['kind']=='dict' and field in lazy_fields):
            data[field]=PartitionedMapping(read_bytes,spec['parts'])
            continue
        for part in spec['parts']:
            entries=_part_values(read_bytes,part)
            if values.keys() & entries.keys():
                raise ValueError("JSON partition count or duplicate record mismatch")
            values.update(entries)
        if spec["kind"] == "list":
            order = spec.get("order")
            if (not isinstance(order, list) or not all(isinstance(key, str) for key in order)
                    or len(order) != len(values)
                    or len(set(order)) != len(order) or set(order) != set(values)):
                raise ValueError("JSON partition list order mismatch")
            data[field] = [values[key] for key in order]
        else:
            data[field] = values
    if manifests is not None:
        manifests[path] = marker["fields"]
    return data
